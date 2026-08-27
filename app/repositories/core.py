"""Shared PostgreSQL connection and row-mapping primitives."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from alembic import command
from alembic.config import Config
import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

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
        return _CursorResult(self._conn, cursor)


class CoreRepository:
    """Connection lifecycle and shared conversion helpers."""

    def __init__(self, database_url: str, schema: str = "monocorpus"):
        self.database_url = str(database_url).strip()
        if not self.database_url:
            raise ValueError("database_url must be non-empty")
        if self.database_url.startswith("postgresql+psycopg2://"):
            self.database_url = "postgresql://" + self.database_url.split("://", 1)[1]
        if self.database_url.startswith("postgresql+psycopg://"):
            self.database_url = "postgresql://" + self.database_url.split("://", 1)[1]
        self.schema = str(schema or "monocorpus").strip() or "monocorpus"
        self._lock = threading.Lock()


    @contextmanager
    def _connect(self) -> Iterable[_ConnectionAdapter]:
        conn = psycopg2.connect(self.database_url)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema))
                )
                cursor.execute(
                    sql.SQL("SET search_path TO {}, public").format(sql.Identifier(self.schema))
                )
            yield _ConnectionAdapter(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


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
