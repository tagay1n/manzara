"""Shared PostgreSQL connection and row-mapping primitives."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

import psycopg2
from alembic.config import Config
from psycopg2 import sql
from psycopg2.extensions import TRANSACTION_STATUS_UNKNOWN
from psycopg2.extras import RealDictCursor

from alembic import command
from app.postgres_engine import (
    acquire_postgres_engine,
    configured_postgres_pool_size,
    get_postgres_engine_metrics,
    record_postgres_query,
    release_postgres_engine,
)

DEFAULT_POOL_SIZE = 4
MAX_POOL_SIZE = 8


class _PersistentConnectionPool:
    """Small blocking pool with lazy creation and deterministic cleanup."""

    def __init__(
        self,
        database_url: str,
        schema: str,
        *,
        max_size: int,
        connection_factory: Callable[[str], psycopg2.extensions.connection],
    ) -> None:
        self._database_url = database_url
        self._schema = schema
        self._max_size = max_size
        self._connection_factory = connection_factory
        self._condition = threading.Condition()
        self._idle: list[psycopg2.extensions.connection] = []
        self._in_use: set[psycopg2.extensions.connection] = set()
        self._connection_count = 0
        self._connections_created = 0
        self._checkouts = 0
        self._queries = 0
        self._closed = False

    @staticmethod
    def _is_broken(conn: psycopg2.extensions.connection) -> bool:
        if bool(getattr(conn, "closed", False)):
            return True
        try:
            return conn.get_transaction_status() == TRANSACTION_STATUS_UNKNOWN
        except Exception:
            return True

    @staticmethod
    def _close_connection(conn: psycopg2.extensions.connection) -> None:
        try:
            conn.close()
        except Exception:
            pass

    def _create_connection(self) -> psycopg2.extensions.connection:
        conn = self._connection_factory(self._database_url)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET search_path TO {}, public").format(
                        sql.Identifier(self._schema)
                    )
                )
            conn.commit()
            return conn
        except Exception:
            self._close_connection(conn)
            raise

    def checkout(self) -> psycopg2.extensions.connection:
        """Wait for a healthy connection without exceeding the configured bound."""
        while True:
            create_new = False
            with self._condition:
                if self._closed:
                    raise RuntimeError("PostgreSQL connection pool is closed")
                while self._idle:
                    conn = self._idle.pop()
                    if not self._is_broken(conn):
                        self._in_use.add(conn)
                        self._checkouts += 1
                        return conn
                    self._connection_count -= 1
                    self._close_connection(conn)
                if self._connection_count < self._max_size:
                    self._connection_count += 1
                    create_new = True
                else:
                    self._condition.wait()

            if not create_new:
                continue
            try:
                conn = self._create_connection()
            except Exception:
                with self._condition:
                    self._connection_count -= 1
                    self._condition.notify()
                raise
            with self._condition:
                if self._closed:
                    self._connection_count -= 1
                    self._close_connection(conn)
                    self._condition.notify_all()
                    raise RuntimeError("PostgreSQL connection pool is closed")
                self._in_use.add(conn)
                self._connections_created += 1
                self._checkouts += 1
                return conn

    def record_query(self) -> None:
        with self._condition:
            self._queries += 1

    def metrics(self) -> Dict[str, int]:
        """Return credential-free cumulative lifecycle counters."""
        with self._condition:
            return {
                "max_size": self._max_size,
                "physical_connections_open": self._connection_count,
                "physical_connections_created": self._connections_created,
                "connections_in_use": len(self._in_use),
                "idle_connections": len(self._idle),
                "checkouts": self._checkouts,
                "queries": self._queries,
            }

    def checkin(
        self,
        conn: psycopg2.extensions.connection,
        *,
        discard: bool = False,
    ) -> None:
        """Return one connection, discarding it when cleanup or transport failed."""
        with self._condition:
            if conn not in self._in_use:
                return
            self._in_use.remove(conn)
            should_close = self._closed or discard or self._is_broken(conn)
            if should_close:
                self._connection_count -= 1
                self._close_connection(conn)
            else:
                self._idle.append(conn)
            self._condition.notify_all()

    def close(self, *, timeout_seconds: float = 5.0) -> None:
        """Stop checkouts, wait briefly for borrowers, then close every connection."""
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._condition:
            self._closed = True
            while self._in_use:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            connections = [*self._idle, *self._in_use]
            self._idle.clear()
            self._in_use.clear()
            self._connection_count = 0
            self._condition.notify_all()
        for conn in connections:
            self._close_connection(conn)


class _SharedEnginePool:
    """Adapt the process-shared SQLAlchemy pool to the core repository API."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self._closed = False

    def checkout(self) -> Any:
        if self._closed:
            raise RuntimeError("PostgreSQL connection pool is closed")
        return self.engine.raw_connection()

    @staticmethod
    def _is_broken(conn: Any) -> bool:
        """Inspect the proxied DBAPI connection without checking it out again."""
        if hasattr(conn, "is_valid") and not bool(conn.is_valid):
            return True
        dbapi_connection = getattr(conn, "dbapi_connection", conn)
        if dbapi_connection is None or bool(getattr(dbapi_connection, "closed", False)):
            return True
        try:
            return (
                dbapi_connection.get_transaction_status()
                == TRANSACTION_STATUS_UNKNOWN
            )
        except Exception:
            return True

    def record_query(self) -> None:
        record_postgres_query(self.engine)

    def metrics(self) -> Dict[str, int]:
        return get_postgres_engine_metrics(self.engine)

    def checkin(self, conn: Any, *, discard: bool = False) -> None:
        if discard:
            try:
                conn.invalidate()
            except Exception:
                pass
        conn.close()

    def close(self, *, timeout_seconds: float = 5.0) -> None:
        del timeout_seconds
        self._closed = True

def utc_now() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


class _CursorResult:
    """SQLite-like cursor result wrapper over psycopg2 cursors."""

    def __init__(self, conn: psycopg2.extensions.connection, cursor: RealDictCursor):
        self._conn = conn
        self._cursor = cursor

    def fetchone(self) -> Optional[Dict[str, Any]]:
        return self._cursor.fetchone()

    def fetchall(self) -> List[Dict[str, Any]]:
        return self._cursor.fetchall()

    def scalar(self) -> Any:
        row = self.fetchone()
        if row is None:
            return None
        return next(iter(row.values()), None)

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount or 0)

    @property
    def lastrowid(self) -> int:
        with self._conn.cursor() as cursor:
            cursor.execute("SELECT LASTVAL()")
            row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def __del__(self) -> None:
        try:
            self._cursor.close()
        except Exception:
            pass


class _ConnectionAdapter:
    """Connection wrapper with qmark placeholder compatibility."""

    def __init__(
        self,
        conn: psycopg2.extensions.connection,
        *,
        on_execute: Optional[Callable[[], None]] = None,
    ):
        self._conn = conn
        self._on_execute = on_execute

    @staticmethod
    def _convert_qmark(query: str) -> str:
        return query.replace("?", "%s")

    def execute(
        self,
        query: str,
        params: Optional[Sequence[Any]] = None,
    ) -> _CursorResult:
        if self._on_execute is not None:
            self._on_execute()
        cursor = self._conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(self._convert_qmark(query), tuple(params or ()))
        return _CursorResult(self._conn, cursor)


class CoreRepository:
    """Connection lifecycle and shared conversion helpers."""

    def __init__(
        self,
        database_url: str,
        schema: str = "monocorpus",
        *,
        pool_size: int | None = None,
        connection_factory: Callable[[str], psycopg2.extensions.connection] | None = None,
    ):
        self.database_url = str(database_url).strip()
        if not self.database_url:
            raise ValueError("database_url must be non-empty")
        if self.database_url.startswith("postgresql+psycopg2://"):
            self.database_url = "postgresql://" + self.database_url.split("://", 1)[1]
        if self.database_url.startswith("postgresql+psycopg://"):
            self.database_url = "postgresql://" + self.database_url.split("://", 1)[1]
        self.schema = str(schema or "monocorpus").strip() or "monocorpus"
        requested_pool_size = (
            configured_postgres_pool_size()
            if pool_size is None
            else int(pool_size)
        )
        if not 1 <= requested_pool_size <= MAX_POOL_SIZE:
            raise ValueError(f"pool_size must be between 1 and {MAX_POOL_SIZE}")
        self.pool_size = requested_pool_size
        if connection_factory is None:
            self._engine = acquire_postgres_engine(
                self.database_url,
                schema=self.schema,
                pool_size=self.pool_size,
            )
            self._pool = _SharedEnginePool(self._engine)
        else:
            self._engine = None
            self._pool = _PersistentConnectionPool(
                self.database_url,
                self.schema,
                max_size=self.pool_size,
                connection_factory=connection_factory,
            )
        self._lock = threading.Lock()
        self._progress_last_published: dict[int, float] = {}


    @contextmanager
    def _connect(self) -> Iterable[_ConnectionAdapter]:
        conn = self._pool.checkout()
        discard = False
        try:
            yield _ConnectionAdapter(conn, on_execute=self._pool.record_query)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                discard = True
            discard = discard or self._pool._is_broken(conn)
            raise
        finally:
            discard = discard or self._pool._is_broken(conn)
            self._pool.checkin(conn, discard=discard)


    def close(self) -> None:
        """Close all persistent PostgreSQL connections."""
        self._pool.close()
        if self._engine is not None:
            release_postgres_engine(self._engine)
            self._engine = None


    def get_pool_metrics(self) -> Dict[str, int]:
        """Return connection/query counters for diagnostics and benchmarks."""
        return self._pool.metrics()


    def init_schema(self) -> None:
        """Apply Alembic migrations up to head for the configured schema."""
        repo_root = Path(__file__).resolve().parents[2]
        alembic_ini = repo_root / "alembic.ini"
        if not alembic_ini.exists():
            raise RuntimeError(f"Alembic config not found: {alembic_ini}")

        config = Config(str(alembic_ini))
        config.set_main_option("manzara_database_url", self.database_url)
        config.set_main_option("manzara_db_schema", self.schema)
        config.set_main_option("manzara_alembic_version_schema", self.schema)

        with self._lock:
            command.upgrade(config, "head")


    def _row_to_task(self, row: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(row)
        payload["command"] = json.loads(payload.pop("command_json"))
        meaningful = payload.pop("meaningful_result_json", "{}")
        try:
            parsed_meaningful = json.loads(meaningful or "{}")
        except Exception:
            parsed_meaningful = {}
        payload["meaningful_result"] = (
            parsed_meaningful if isinstance(parsed_meaningful, dict) else {}
        )
        return payload


    def _decode_summary(self, raw_summary: Any) -> Dict[str, Any]:
        text = str(raw_summary or "").strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}


    def _row_to_run(self, row: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(row)
        payload["summary"] = self._decode_summary(payload.pop("summary_json", "{}"))
        payload["progress"] = self._decode_summary(payload.pop("progress_json", "{}"))
        return payload


    @staticmethod
    def _normalize_id_list(values: Sequence[str]) -> List[str]:
        normalized: List[str] = []
        seen: set[str] = set()
        for raw in values:
            value = str(raw or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized


    @staticmethod
    def _placeholders(count: int) -> str:
        return ", ".join("?" for _ in range(max(0, int(count))))


    def _select_obsolete_ids(
        self,
        conn: _ConnectionAdapter,
        *,
        table: str,
        id_column: str,
        keep_ids: Sequence[str],
    ) -> List[str]:
        keep = self._normalize_id_list(keep_ids)
        if keep:
            rows = conn.execute(
                f"SELECT {id_column} FROM {table} WHERE {id_column} NOT IN ({self._placeholders(len(keep))})",
                keep,
            ).fetchall()
        else:
            rows = conn.execute(f"SELECT {id_column} FROM {table}").fetchall()
        return [str(row[id_column]) for row in rows if str(row.get(id_column) or "").strip()]


    def _delete_in(
        self,
        conn: _ConnectionAdapter,
        *,
        table: str,
        id_column: str,
        values: Sequence[Any],
    ) -> int:
        items = [value for value in values if value is not None]
        if not items:
            return 0
        cur = conn.execute(
            f"DELETE FROM {table} WHERE {id_column} IN ({self._placeholders(len(items))})",
            items,
        )
        return int(cur.rowcount or 0)
