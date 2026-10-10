"""Shared durable PostgreSQL and disposable local-state primitives."""

from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import psycopg2
from psycopg2.extensions import TRANSACTION_STATUS_UNKNOWN
from psycopg2.extras import RealDictCursor

from app.artifacts import local_state_path as configured_local_state_path
from app.local_state import LocalStateStore
from app.postgres_engine import (
    acquire_postgres_engine,
    configured_postgres_pool_size,
    configured_timeout_sql,
    get_postgres_engine_metrics,
    release_postgres_engine,
)
from app.settings import configured_schema

MAX_POOL_SIZE = 8


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

    def metrics(self) -> Dict[str, int]:
        return get_postgres_engine_metrics(self.engine)

    def checkin(self, conn: Any, *, discard: bool = False) -> None:
        if discard:
            try:
                conn.invalidate()
            except Exception:
                pass
        conn.close()

    def close(self) -> None:
        self._closed = True

def utc_now() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


class _CursorResult:
    """SQLite-like cursor result wrapper over psycopg2 cursors."""

    def __init__(self, cursor: RealDictCursor):
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

    def __del__(self) -> None:
        try:
            self._cursor.close()
        except Exception:
            pass


class _ConnectionAdapter:
    """Connection wrapper with qmark placeholder compatibility."""

    def __init__(self, conn: psycopg2.extensions.connection):
        self._conn = conn

    @staticmethod
    def _convert_qmark(query: str) -> str:
        return query.replace("?", "%s")

    def execute(
        self,
        query: str,
        params: Optional[Sequence[Any]] = None,
    ) -> _CursorResult:
        cursor = self._conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(self._convert_qmark(query), tuple(params or ()))
        return _CursorResult(cursor)


class CoreRepository:
    """Connection lifecycle and shared conversion helpers."""

    def __init__(
        self,
        database_url: str,
        schema: str | None = None,
        *,
        pool_size: int | None = None,
        local_state_path: Path | str | None = None,
    ):
        self.database_url = str(database_url).strip()
        if not self.database_url:
            raise ValueError("database_url must be non-empty")
        if self.database_url.startswith("postgresql+psycopg2://"):
            self.database_url = "postgresql://" + self.database_url.split("://", 1)[1]
        if self.database_url.startswith("postgresql+psycopg://"):
            self.database_url = "postgresql://" + self.database_url.split("://", 1)[1]
        self.schema = configured_schema("database_schema") if schema is None else schema.strip()
        requested_pool_size = (
            configured_postgres_pool_size()
            if pool_size is None
            else pool_size
        )
        if type(requested_pool_size) is not int or not 1 <= requested_pool_size <= MAX_POOL_SIZE:
            raise ValueError(f"pool_size must be between 1 and {MAX_POOL_SIZE}")
        self.pool_size = requested_pool_size
        self._engine = acquire_postgres_engine(
            self.database_url,
            schema=self.schema,
            pool_size=self.pool_size,
        )
        self._pool = _SharedEnginePool(self._engine)
        self._lock = threading.Lock()
        self._progress_last_published: dict[int, float] = {}
        self._local_state = LocalStateStore(
            local_state_path
            if local_state_path is not None
            else configured_local_state_path()
        )


    @contextmanager
    def _connect(self) -> Iterable[_ConnectionAdapter]:
        conn = self._pool.checkout()
        discard = False
        try:
            yield _ConnectionAdapter(conn)
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


    @contextmanager
    def _runtime_connect(self, *, immediate: bool = False) -> Iterable[Any]:
        """Open one short transaction against mandatory machine-local state."""
        with self._local_state.connect(immediate=immediate) as conn:
            yield conn


    def close(self) -> None:
        """Close all persistent PostgreSQL connections."""
        self._pool.close()
        if self._engine is not None:
            release_postgres_engine(self._engine)
            self._engine = None


    def get_pool_metrics(self) -> Dict[str, int]:
        """Return connection/query counters for diagnostics and benchmarks."""
        return self._pool.metrics()


    @property
    def local_state_path(self) -> Path:
        return self._local_state.path


    def init_local_state(self) -> None:
        """Initialize disposable runtime state without migrating PostgreSQL."""
        self._local_state.initialize()

    def check_personality_catalog(self) -> None:
        """Read-only preflight for the supported normalized catalog contract."""
        required = {
            "catalog_publications": {"publication_id", "inclusion", "has_metadata", "metadata_present"},
            "catalog_publication_languages": {"publication_id", "position", "language"},
            "catalog_documents": {"md5", "publication_id"},
            "catalog_names": {"name_id", "kind", "raw_name"},
            "catalog_entities": {"entity_id", "kind", "approval", "identity_key", "revision"},
            "catalog_aliases": {"alias_id", "name_id", "entity_id", "approval", "revision"},
            "catalog_alias_reviews": {"alias_id", "name_id", "entity_id", "successful_model", "source_roles"},
            "catalog_entity_roles": {"entity_id", "role"},
            "catalog_contributions": {"contribution_id", "publication_id", "name_id", "role", "position", "nested_position"},
            "catalog_credit_groups": {"publication_id", "role", "position"},
            "catalog_protections": {"record_kind", "record_key", "field"},
            "catalog_revisions": {"record_kind", "record_key", "actor", "before", "after"},
            "personality_normalization_checkpoints": {"raw_name", "canonical_id", "source_fingerprint", "state", "decision_reason", "decision_evidence"},
        }
        with self._connect() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute(configured_timeout_sql("statement_timeout", "preflight_timeout_seconds"))
            rows = conn.execute(
                "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=? AND table_name=ANY(?)",
                (self.schema, list(required)),
            ).fetchall()
            present: dict[str, set[str]] = {}
            for row in rows:
                present.setdefault(row["table_name"], set()).add(row["column_name"])
            missing = [f"{table}.{column}" for table, columns in required.items()
                       for column in sorted(columns - present.get(table, set()))]
            if missing:
                raise RuntimeError("Catalog is incompatible with personality normalization; missing: " + ", ".join(missing))
            version_schema = str(configured_schema("migration_version_schema"))
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", version_schema):
                raise ValueError("Invalid migration version schema")
            revision = conn.execute(f'SELECT version_num FROM "{version_schema}".alembic_version_manzara').scalar()
            if not re.fullmatch(r"\d{8}_\d{4}", str(revision)) or int(str(revision).split("_")[1]) < 62:
                raise RuntimeError("Catalog revision 20261008_0062 or later is required; apply migrations separately.")


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
        payload["provider_wait"] = self._decode_summary(payload.pop("provider_wait_json"))
        return payload
