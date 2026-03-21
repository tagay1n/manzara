"""SQLite access layer for Manzara."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ACTIVE_STATUSES = (
    "starting",
    "running",
    "stopping_graceful",
    "stopping_force",
)


def utc_now() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Thin repository layer over SQLite with thread-safe writes."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()

    @contextmanager
    def _connect(self) -> Iterable[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        """Create required tables and indexes if they do not exist."""
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_definitions (
                    task_id TEXT PRIMARY KEY,
                    panel_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    icon_idle TEXT NOT NULL,
                    icon_running TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    panel_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stop_mode TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    heartbeat_at TEXT,
                    pid INTEGER,
                    exit_code INTEGER,
                    progress_current INTEGER,
                    progress_total INTEGER,
                    error_text TEXT,
                    FOREIGN KEY (task_id) REFERENCES task_definitions(task_id)
                );

                CREATE TABLE IF NOT EXISTS run_logs (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    ts TEXT NOT NULL,
                    stream TEXT NOT NULL,
                    line TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    type TEXT NOT NULL,
                    task_id TEXT,
                    run_id INTEGER,
                    panel_id TEXT,
                    payload_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_runs_task_status ON runs(task_id, status);
                CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
                CREATE INDEX IF NOT EXISTS idx_run_logs_run_id ON run_logs(run_id, log_id);
                CREATE INDEX IF NOT EXISTS idx_events_event_id ON events(event_id);
                """
            )

    def seed_tasks(self, task_defs: List[Dict[str, Any]]) -> None:
        """Insert or update known task definitions."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                for item in task_defs:
                    conn.execute(
                        """
                        INSERT INTO task_definitions (
                            task_id, panel_id, title, task_type,
                            icon_idle, icon_running, command_json, cwd,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(task_id) DO UPDATE SET
                            panel_id=excluded.panel_id,
                            title=excluded.title,
                            task_type=excluded.task_type,
                            icon_idle=excluded.icon_idle,
                            icon_running=excluded.icon_running,
                            command_json=excluded.command_json,
                            cwd=excluded.cwd,
                            updated_at=excluded.updated_at
                        """,
                        (
                            item["task_id"],
                            item["panel_id"],
                            item["title"],
                            item["task_type"],
                            item["icon_idle"],
                            item["icon_running"],
                            json.dumps(item["command"]),
                            item["cwd"],
                            now,
                            now,
                        ),
                    )

    def _row_to_task(self, row: sqlite3.Row) -> Dict[str, Any]:
        payload = dict(row)
        payload["command"] = json.loads(payload.pop("command_json"))
        return payload

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Return task definition by id."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_definitions WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(self) -> List[Dict[str, Any]]:
        """Return all task definitions."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_definitions ORDER BY panel_id, title"
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def create_run(self, task: Dict[str, Any]) -> int:
        """Create a new run in starting state and return run id."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO runs (
                        task_id, panel_id, status, stop_mode,
                        started_at, heartbeat_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task["task_id"],
                        task["panel_id"],
                        "starting",
                        None,
                        now,
                        now,
                    ),
                )
                return int(cur.lastrowid)

    def mark_run_started(self, run_id: int, pid: int) -> None:
        """Set run state to running with process id."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE runs
                    SET status = ?, pid = ?, heartbeat_at = ?
                    WHERE run_id = ?
                    """,
                    ("running", pid, now, run_id),
                )

    def heartbeat(self, run_id: int) -> None:
        """Update run heartbeat timestamp."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE runs SET heartbeat_at = ? WHERE run_id = ?",
                    (now, run_id),
                )

    def set_stop_mode(self, run_id: int, mode: str) -> bool:
        """Move an active run into graceful or force stopping mode."""
        status = "stopping_graceful" if mode == "graceful" else "stopping_force"
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    f"""
                    UPDATE runs
                    SET stop_mode = ?, status = ?, heartbeat_at = ?
                    WHERE run_id = ?
                      AND status IN ({placeholders})
                    """,
                    (mode, status, utc_now(), run_id, *ACTIVE_STATUSES),
                )
                return int(cur.rowcount or 0) > 0

    def finish_run(
        self,
        run_id: int,
        status: str,
        exit_code: Optional[int],
        error_text: Optional[str],
    ) -> None:
        """Finalize a run outcome."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE runs
                    SET status = ?, exit_code = ?, error_text = ?,
                        finished_at = ?, heartbeat_at = ?
                    WHERE run_id = ?
                    """,
                    (status, exit_code, error_text, now, now, run_id),
                )

    def append_log(self, run_id: int, stream: str, line: str) -> int:
        """Append one log line and return inserted log id."""
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO run_logs (run_id, ts, stream, line)
                    VALUES (?, ?, ?, ?)
                    """,
                    (run_id, utc_now(), stream, line),
                )
                return int(cur.lastrowid)

    def insert_event(
        self,
        event_type: str,
        task_id: Optional[str],
        run_id: Optional[int],
        panel_id: Optional[str],
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Persist an event row and return serialized event object."""
        timestamp = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO events (ts, type, task_id, run_id, panel_id, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        timestamp,
                        event_type,
                        task_id,
                        run_id,
                        panel_id,
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
                event_id = int(cur.lastrowid)
        return {
            "event_id": event_id,
            "ts": timestamp,
            "type": event_type,
            "task_id": task_id,
            "run_id": run_id,
            "panel_id": panel_id,
            "payload": payload,
        }

    def get_events_after(self, after_event_id: int, limit: int = 200) -> List[Dict[str, Any]]:
        """Return events with id greater than marker."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, ts, type, task_id, run_id, panel_id, payload_json
                FROM events
                WHERE event_id > ?
                ORDER BY event_id ASC
                LIMIT ?
                """,
                (after_event_id, limit),
            ).fetchall()
        events: List[Dict[str, Any]] = []
        for row in rows:
            events.append(
                {
                    "event_id": row["event_id"],
                    "ts": row["ts"],
                    "type": row["type"],
                    "task_id": row["task_id"],
                    "run_id": row["run_id"],
                    "panel_id": row["panel_id"],
                    "payload": json.loads(row["payload_json"]),
                }
            )
        return events

    def get_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        """Return one run by id."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def get_logs(self, run_id: int, after_log_id: int = 0, limit: int = 300) -> List[Dict[str, Any]]:
        """Return run log lines after marker."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT log_id, run_id, ts, stream, line
                FROM run_logs
                WHERE run_id = ? AND log_id > ?
                ORDER BY log_id ASC
                LIMIT ?
                """,
                (run_id, after_log_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_active_runs(self) -> List[Dict[str, Any]]:
        """Return active runs across all tasks."""
        with self._connect() as conn:
            placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
            rows = conn.execute(
                f"""
                SELECT * FROM runs
                WHERE status IN ({placeholders})
                ORDER BY started_at DESC
                """,
                ACTIVE_STATUSES,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_active_run_for_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Return active run for task, if any."""
        with self._connect() as conn:
            placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
            row = conn.execute(
                f"""
                SELECT * FROM runs
                WHERE task_id = ? AND status IN ({placeholders})
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (task_id, *ACTIVE_STATUSES),
            ).fetchone()
        return dict(row) if row else None

    def list_tasks_with_latest_run(self) -> List[Dict[str, Any]]:
        """Return each task with latest run details if available."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    t.task_id,
                    t.panel_id,
                    t.title,
                    t.task_type,
                    t.icon_idle,
                    t.icon_running,
                    t.command_json,
                    t.cwd,
                    r.run_id,
                    r.status AS run_status,
                    r.stop_mode,
                    r.started_at,
                    r.finished_at,
                    r.heartbeat_at,
                    r.exit_code,
                    r.error_text
                FROM task_definitions t
                LEFT JOIN runs r
                    ON r.run_id = (
                        SELECT run_id
                        FROM runs r2
                        WHERE r2.task_id = t.task_id
                        ORDER BY r2.run_id DESC
                        LIMIT 1
                    )
                ORDER BY t.panel_id, t.title
                """
            ).fetchall()

        items: List[Dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["command"] = json.loads(payload.pop("command_json"))
            items.append(payload)
        return items

    def list_recent_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return recent runs for dashboard and quick inspection."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, task_id, panel_id, status, stop_mode,
                       started_at, finished_at, heartbeat_at,
                       pid, exit_code, error_text
                FROM runs
                ORDER BY run_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def run_count_by_status(self, panel_id: str) -> Dict[str, int]:
        """Return run status counters for one panel."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM runs
                WHERE panel_id = ?
                GROUP BY status
                """,
                (panel_id,),
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def last_successful_run(self, panel_id: str) -> Optional[str]:
        """Return timestamp of most recent successful run."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT finished_at
                FROM runs
                WHERE panel_id = ? AND status = 'completed'
                ORDER BY run_id DESC
                LIMIT 1
                """,
                (panel_id,),
            ).fetchone()
        if not row:
            return None
        return row["finished_at"]

    def recover_active_runs(self) -> int:
        """Mark previously active runs as failed after process restart."""
        now = utc_now()
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    f"""
                    UPDATE runs
                    SET status = 'failed',
                        finished_at = ?,
                        heartbeat_at = ?,
                        error_text = COALESCE(
                            error_text,
                            'Recovered after Manzara restart; previous process state is unknown.'
                        )
                    WHERE status IN ({placeholders})
                    """,
                    (now, now, *ACTIVE_STATUSES),
                )
                return int(cur.rowcount or 0)
