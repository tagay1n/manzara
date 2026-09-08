"""Machine-local SQLite state for disposable Manzara runtime data."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

LOCAL_STATE_SCHEMA_VERSION = 1
_FOR_UPDATE_RE = re.compile(r"\s+FOR\s+UPDATE\b", re.IGNORECASE)


class _SQLiteResult:
    def __init__(self, cursor: sqlite3.Cursor):
        self._cursor = cursor

    @staticmethod
    def _row(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
        return dict(row) if row is not None else None

    def fetchone(self) -> Optional[dict[str, Any]]:
        return self._row(self._cursor.fetchone())

    def fetchall(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._cursor.fetchall()]

    def scalar(self) -> Any:
        row = self.fetchone()
        return next(iter(row.values()), None) if row else None

    @property
    def rowcount(self) -> int:
        return max(0, int(self._cursor.rowcount or 0))

    @property
    def lastrowid(self) -> int:
        return int(self._cursor.lastrowid or 0)


class _SQLiteConnection:
    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    def execute(
        self, query: str, params: Optional[Sequence[Any]] = None
    ) -> _SQLiteResult:
        normalized = _FOR_UPDATE_RE.sub("", query)
        return _SQLiteResult(self._connection.execute(normalized, tuple(params or ())))


class LocalStateStore:
    """Own SQLite initialization and short cross-process transactions."""

    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        with self.connect() as conn:
            version = int(conn.execute("PRAGMA user_version").scalar() or 0)
            if version not in (0, LOCAL_STATE_SCHEMA_VERSION):
                raise RuntimeError(
                    f"Unsupported local runtime schema version {version}; "
                    f"expected {LOCAL_STATE_SCHEMA_VERSION}"
                )
            conn._connection.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {LOCAL_STATE_SCHEMA_VERSION}")
            # IDs remain unique against retained run artifacts if the disposable
            # database is recreated on the same laptop.
            floor = int(time.time() * 1000)
            for table in ("runs", "events", "conveyor_runs"):
                updated = conn.execute(
                    "UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name=?",
                    (floor, table),
                )
                if updated.rowcount == 0:
                    conn.execute(
                        "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                        (table, floor),
                    )
        self.path.chmod(0o600)

    @contextmanager
    def connect(self, *, immediate: bool = False) -> Iterable[_SQLiteConnection]:
        try:
            connection = sqlite3.connect(
                str(self.path), timeout=5.0, isolation_level="DEFERRED"
            )
        except sqlite3.Error as exc:
            raise RuntimeError(f"Cannot open local runtime database: {exc}") from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield _SQLiteConnection(connection)
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise RuntimeError(f"Local runtime database operation failed: {exc}") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class AIItemCheckpointStore:
    """Generic laptop-local retry state shared by AI workflows."""

    def __init__(self, path: Path | str):
        self._store = LocalStateStore(path)

    def get(self, flow_id: str, item_id: str) -> dict[str, Any] | None:
        with self._store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM ai_item_checkpoints WHERE flow_id=? AND item_id=?",
                (str(flow_id), str(item_id)),
            ).fetchone()
        return self._decode(row)

    def get_many(
        self, flow_id: str, item_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        normalized = tuple(dict.fromkeys(str(item_id) for item_id in item_ids))
        if not normalized:
            return {}
        rows: list[dict[str, Any]] = []
        with self._store.connect() as conn:
            for offset in range(0, len(normalized), 500):
                chunk = normalized[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows.extend(
                    conn.execute(
                        "SELECT * FROM ai_item_checkpoints "
                        f"WHERE flow_id=? AND item_id IN ({placeholders})",
                        (str(flow_id), *chunk),
                    ).fetchall()
                )
        decoded = (self._decode(row) for row in rows)
        return {str(row["item_id"]): row for row in decoded if row is not None}

    def record_failure(
        self,
        *,
        flow_id: str,
        item_id: str,
        contract_version: str,
        model_name: str,
        kind: str,
        error: str,
        models: Sequence[str],
        run_id: int | None,
    ) -> None:
        attempt = {
            "model": str(model_name),
            "kind": str(kind),
            "error": str(error or "")[:4000],
            "recorded_at": _utc_now(),
        }
        with self._store.connect(immediate=True) as conn:
            existing = conn.execute(
                "SELECT contract_version, attempts_json FROM ai_item_checkpoints "
                "WHERE flow_id=? AND item_id=?",
                (flow_id, item_id),
            ).fetchone()
            attempts = []
            if existing and existing["contract_version"] == contract_version:
                attempts = json.loads(existing["attempts_json"] or "[]")
            if not any(str(item.get("model") or "") == model_name for item in attempts):
                attempts.append(attempt)
            self._upsert(
                conn,
                flow_id=flow_id,
                item_id=item_id,
                contract_version=contract_version,
                status="partial",
                attempts=attempts,
                models=models,
                retry_after=None,
                failure_count=0,
                last_error=None,
                terminal_reason=None,
                run_id=run_id,
            )

    def record_deferral(
        self, *, flow_id: str, item_id: str, contract_version: str,
        models: Sequence[str], retry_after: str, error: str, run_id: int | None,
    ) -> None:
        with self._store.connect(immediate=True) as conn:
            existing = self._read(conn, flow_id, item_id) or {}
            attempts = (
                existing.get("attempts")
                if existing.get("contract_version") == contract_version
                else []
            )
            failure_count = (
                int(existing.get("operational_failure_count") or 0) + 1
                if existing.get("contract_version") == contract_version
                else 1
            )
            self._upsert(
                conn, flow_id=flow_id, item_id=item_id,
                contract_version=contract_version, status="partial",
                attempts=attempts or [], models=models, retry_after=retry_after,
                failure_count=failure_count, last_error=str(error or "")[:4000],
                terminal_reason=None, run_id=run_id,
            )

    def mark_terminal(
        self, *, flow_id: str, item_id: str, contract_version: str,
        models: Sequence[str], reason: str, run_id: int | None,
    ) -> None:
        with self._store.connect(immediate=True) as conn:
            existing = self._read(conn, flow_id, item_id) or {}
            attempts = (
                existing.get("attempts")
                if existing.get("contract_version") == contract_version
                else []
            )
            self._upsert(
                conn, flow_id=flow_id, item_id=item_id,
                contract_version=contract_version, status="terminal",
                attempts=attempts or [], models=models, retry_after=None,
                failure_count=0, last_error=None,
                terminal_reason=str(reason or "")[:4000], run_id=run_id,
            )

    def clear(self, flow_id: str, item_id: str) -> None:
        with self._store.connect(immediate=True) as conn:
            conn.execute(
                "DELETE FROM ai_item_checkpoints WHERE flow_id=? AND item_id=?",
                (flow_id, item_id),
            )

    @staticmethod
    def _upsert(conn: _SQLiteConnection, **values: Any) -> None:
        conn.execute(
            """INSERT INTO ai_item_checkpoints (
                   flow_id,item_id,contract_version,status,attempts_json,
                   model_pool_json,retry_after,operational_failure_count,last_error,
                   terminal_reason,run_id,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(flow_id,item_id) DO UPDATE SET
                   contract_version=excluded.contract_version,status=excluded.status,
                   attempts_json=excluded.attempts_json,
                   model_pool_json=excluded.model_pool_json,retry_after=excluded.retry_after,
                   operational_failure_count=excluded.operational_failure_count,
                   last_error=excluded.last_error,terminal_reason=excluded.terminal_reason,
                   run_id=excluded.run_id,updated_at=excluded.updated_at""",
            (
                values["flow_id"],
                values["item_id"], values["contract_version"], values["status"],
                json.dumps(values["attempts"], ensure_ascii=False),
                json.dumps(list(values["models"]), ensure_ascii=False),
                values["retry_after"], values["failure_count"], values["last_error"],
                values["terminal_reason"], values["run_id"], _utc_now(),
            ),
        )

    @classmethod
    def _read(
        cls, conn: _SQLiteConnection, flow_id: str, item_id: str
    ) -> dict[str, Any] | None:
        return cls._decode(
            conn.execute(
                "SELECT * FROM ai_item_checkpoints WHERE flow_id=? AND item_id=?",
                (str(flow_id), str(item_id)),
            ).fetchone()
        )

    @staticmethod
    def _decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is not None:
            row["attempts"] = json.loads(row.pop("attempts_json") or "[]")
            row["model_pool"] = json.loads(row.pop("model_pool_json") or "[]")
        return row


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS panel_definitions (
    panel_id TEXT PRIMARY KEY, title TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_definitions (
    task_id TEXT PRIMARY KEY, panel_id TEXT NOT NULL, title TEXT NOT NULL,
    task_type TEXT NOT NULL, icon_idle TEXT NOT NULL, icon_running TEXT NOT NULL,
    command_json TEXT NOT NULL, cwd TEXT NOT NULL,
    meaningful_result_json TEXT NOT NULL DEFAULT '{}',
    gemini_workers_default INTEGER, gemini_workers_next INTEGER,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    panel_id TEXT NOT NULL, status TEXT NOT NULL, stop_mode TEXT, pid INTEGER,
    started_at TEXT NOT NULL, finished_at TEXT, heartbeat_at TEXT,
    exit_code INTEGER, error_text TEXT, summary_json TEXT NOT NULL DEFAULT '{}',
    progress_json TEXT NOT NULL DEFAULT '{}', gemini_workers INTEGER,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES task_definitions(task_id)
);
CREATE INDEX IF NOT EXISTS idx_runs_task_status ON runs(task_id, status);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
    task_id TEXT, run_id INTEGER, panel_id TEXT, ts TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conveyor_definitions (
    conveyor_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
    stages_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conveyor_runs (
    conveyor_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    definition_revision INTEGER NOT NULL, status TEXT NOT NULL, outcome TEXT,
    started_at TEXT NOT NULL, finished_at TEXT, stop_requested INTEGER NOT NULL DEFAULT 0,
    error_text TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_conveyor_one_active_run
ON conveyor_runs((1)) WHERE status IN ('starting', 'running');
CREATE TABLE IF NOT EXISTS conveyor_run_items (
    conveyor_run_id INTEGER NOT NULL, item_id TEXT NOT NULL, stage_id TEXT NOT NULL,
    stage_order INTEGER NOT NULL, task_order INTEGER NOT NULL, task_id TEXT NOT NULL,
    status TEXT NOT NULL, task_run_id INTEGER, meaningful INTEGER,
    output_json TEXT NOT NULL DEFAULT '{}', error_text TEXT,
    started_at TEXT, finished_at TEXT,
    PRIMARY KEY(conveyor_run_id, item_id),
    FOREIGN KEY(conveyor_run_id) REFERENCES conveyor_runs(conveyor_run_id) ON DELETE CASCADE,
    FOREIGN KEY(task_run_id) REFERENCES runs(run_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_conveyor_run_items_stage
ON conveyor_run_items(conveyor_run_id, stage_order, task_order);
CREATE TABLE IF NOT EXISTS gemini_keys (
    key_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, masked_key TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gemini_keys_account ON gemini_keys(account_id);
CREATE TABLE IF NOT EXISTS gemini_key_model_state (
    key_id TEXT NOT NULL, model_name TEXT NOT NULL, exhausted INTEGER NOT NULL DEFAULT 0,
    exhausted_at TEXT, cooldown_until TEXT, last_used_at TEXT, last_success_at TEXT,
    last_error_at TEXT, last_error_text TEXT, attempts_total INTEGER NOT NULL DEFAULT 0,
    attempts_cycle INTEGER NOT NULL DEFAULT 0, success_total INTEGER NOT NULL DEFAULT 0,
    success_cycle INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
    PRIMARY KEY(key_id, model_name),
    FOREIGN KEY(key_id) REFERENCES gemini_keys(key_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_gemini_state_model
ON gemini_key_model_state(model_name, exhausted);
CREATE TABLE IF NOT EXISTS gemini_runtime_control (
    control_id INTEGER PRIMARY KEY, cycle_label TEXT NOT NULL, pause_until TEXT,
    last_pause_reason TEXT, blackout_override_until TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gemini_account_leases (
    account_id TEXT PRIMARY KEY, lease_token TEXT, task_id TEXT, run_id INTEGER,
    worker_id TEXT, lease_expires_at TEXT, last_acquired_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gemini_model_runtime (
    model_name TEXT PRIMARY KEY, pause_until TEXT, last_pause_reason TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_item_checkpoints (
    flow_id TEXT NOT NULL, item_id TEXT NOT NULL, contract_version TEXT NOT NULL,
    status TEXT NOT NULL, attempts_json TEXT NOT NULL DEFAULT '[]',
    model_pool_json TEXT NOT NULL DEFAULT '[]', retry_after TEXT,
    operational_failure_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT, terminal_reason TEXT, run_id INTEGER, updated_at TEXT NOT NULL,
    PRIMARY KEY(flow_id, item_id)
);
"""


__all__ = ["AIItemCheckpointStore", "LOCAL_STATE_SCHEMA_VERSION", "LocalStateStore"]
