from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.repositories.core import utc_now
from app.runtime_states import (
    TASK_RUN_ACTIVE_STATUSES as ACTIVE_STATUSES,
    TASK_RUN_STATUS_FAILED,
    TASK_RUN_STATUS_RUNNING,
    TASK_RUN_STATUS_STARTING,
    task_status_from_stop_mode,
)


class RunRepository:
    """PostgreSQL operations for the runs domain."""

    def get_latest_run_for_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Return most recent run for task."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM runs
                WHERE task_id = ?
                ORDER BY run_id DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        return self._row_to_run(row) if row else None


    def create_run(self, task: Dict[str, Any]) -> int:
        """Create a run and atomically consume its one-shot worker override."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                workers = None
                if task.get("gemini_workers_default") is not None:
                    row = conn.execute(
                        """SELECT gemini_workers_default, gemini_workers_next
                           FROM task_definitions WHERE task_id = ? FOR UPDATE""",
                        (task["task_id"],),
                    ).fetchone()
                    if row:
                        workers = int(
                            row.get("gemini_workers_next")
                            or row.get("gemini_workers_default")
                            or 1
                        )
                        conn.execute(
                            """UPDATE task_definitions SET gemini_workers_next = NULL,
                               updated_at = ? WHERE task_id = ?""",
                            (now, task["task_id"]),
                        )
                cur = conn.execute(
                    """
                    INSERT INTO runs (
                        task_id, panel_id, status, stop_mode,
                        started_at, heartbeat_at, created_at, updated_at, summary_json,
                        gemini_workers
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task["task_id"],
                        task["panel_id"],
                        TASK_RUN_STATUS_STARTING,
                        None,
                        now,
                        now,
                        now,
                        now,
                        "{}",
                        workers,
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
                    SET status = ?, pid = ?, heartbeat_at = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (TASK_RUN_STATUS_RUNNING, pid, now, now, run_id),
                )


    def heartbeat(self, run_id: int) -> None:
        """Update run heartbeat timestamp."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE runs SET heartbeat_at = ?, updated_at = ? WHERE run_id = ?",
                    (now, now, run_id),
                )


    def update_run_progress(self, run_id: int, progress: Dict[str, Any]) -> None:
        """Persist the latest authoritative progress snapshot for a run."""
        if not isinstance(progress, dict):
            raise ValueError("progress must be an object")
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE runs
                    SET progress_json = ?, heartbeat_at = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (json.dumps(progress, ensure_ascii=False), now, now, int(run_id)),
                )


    def set_stop_mode(self, run_id: int, mode: str) -> bool:
        """Move an active run into graceful or force stopping mode."""
        status = task_status_from_stop_mode(mode)
        now = utc_now()
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    f"""
                    UPDATE runs
                    SET stop_mode = ?, status = ?, heartbeat_at = ?, updated_at = ?
                    WHERE run_id = ?
                      AND status IN ({placeholders})
                    """,
                    (mode, status, now, now, run_id, *ACTIVE_STATUSES),
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
                        finished_at = ?, heartbeat_at = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (status, exit_code, error_text, now, now, now, run_id),
                )


    def update_run_summary(self, run_id: int, summary: Dict[str, Any]) -> None:
        """Persist structured summary payload for one run."""
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE runs
                    SET summary_json = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        json.dumps(summary or {}, ensure_ascii=False),
                        utc_now(),
                        run_id,
                    ),
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


    def get_latest_event_id(self) -> int:
        """Return the current end cursor for the operational event stream."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(event_id), 0) AS event_id FROM events"
            ).fetchone()
        return int(row["event_id"] or 0) if row else 0


    def get_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        """Return one run by id."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None


    def get_logs(
        self,
        run_id: int,
        after_log_id: int = 0,
        limit: int = 300,
        *,
        before_log_id: Optional[int] = None,
        tail: bool = False,
    ) -> List[Dict[str, Any]]:
        """Return run log lines using cursor pagination (after/before) or tail mode."""
        limit = max(1, min(int(limit), 5000))
        with self._connect() as conn:
            if before_log_id is not None and int(before_log_id) > 0:
                rows = conn.execute(
                    """
                    SELECT log_id, run_id, ts, stream, line
                    FROM run_logs
                    WHERE run_id = ? AND log_id < ?
                    ORDER BY log_id DESC
                    LIMIT ?
                    """,
                    (run_id, int(before_log_id), limit),
                ).fetchall()
                lines = [dict(row) for row in rows]
                lines.reverse()
                return lines

            if tail:
                rows = conn.execute(
                    """
                    SELECT log_id, run_id, ts, stream, line
                    FROM run_logs
                    WHERE run_id = ?
                    ORDER BY log_id DESC
                    LIMIT ?
                    """,
                    (run_id, limit),
                ).fetchall()
                lines = [dict(row) for row in rows]
                lines.reverse()
                return lines

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


    def has_logs_before(self, run_id: int, log_id: int) -> bool:
        """Return True when older logs exist before cursor."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 AS has_row
                FROM run_logs
                WHERE run_id = ? AND log_id < ?
                LIMIT 1
                """,
                (run_id, int(log_id)),
            ).fetchone()
        return row is not None


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
        return [self._row_to_run(row) for row in rows]


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
        return self._row_to_run(row) if row else None


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
                    t.gemini_workers_default,
                    t.gemini_workers_next,
                    r.run_id,
                    r.status AS run_status,
                    r.stop_mode,
                    r.started_at,
                    r.finished_at,
                    r.heartbeat_at,
                    r.exit_code,
                    r.error_text,
                    r.summary_json,
                    r.progress_json,
                    r.gemini_workers
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
            payload["run_summary"] = self._decode_summary(payload.pop("summary_json", "{}"))
            payload["run_progress"] = self._decode_summary(payload.pop("progress_json", "{}"))
            items.append(payload)
        return items


    def list_recent_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return recent runs for dashboard and quick inspection."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, task_id, panel_id, status, stop_mode,
                       started_at, finished_at, heartbeat_at,
                       pid, exit_code, error_text, summary_json, progress_json,
                       gemini_workers
                FROM runs
                ORDER BY run_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]


    def list_recent_runs_for_task(self, task_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Return recent runs for one task."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, task_id, panel_id, status, stop_mode,
                       started_at, finished_at, heartbeat_at,
                       pid, exit_code, error_text, summary_json, progress_json,
                       gemini_workers
                FROM runs
                WHERE task_id = ?
                ORDER BY run_id DESC
                LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]


    def get_database_storage_snapshot(
        self,
        *,
        schema_name: Optional[str] = None,
        table_limit: int = 200,
    ) -> Dict[str, Any]:
        """Return PostgreSQL storage snapshot for dashboard diagnostics."""
        target_schema = str(schema_name or self.schema or "public").strip() or "public"
        limit = max(1, min(int(table_limit), 500))
        with self._connect() as conn:
            database_name = conn.execute("SELECT current_database() AS name").scalar()
            database_size_bytes = conn.execute(
                "SELECT pg_database_size(current_database()) AS bytes"
            ).scalar()
            rows = conn.execute(
                """
                SELECT
                    c.relname AS table_name,
                    COALESCE(
                        NULLIF(s.n_live_tup, -1),
                        GREATEST(c.reltuples::bigint, 0)
                    )::bigint AS estimated_rows,
                    pg_total_relation_size(c.oid)::bigint AS total_bytes
                FROM pg_class c
                JOIN pg_namespace n
                    ON n.oid = c.relnamespace
                LEFT JOIN pg_stat_user_tables s
                    ON s.relid = c.oid
                WHERE c.relkind = 'r'
                  AND n.nspname = ?
                ORDER BY total_bytes DESC, c.relname ASC
                LIMIT ?
                """,
                (target_schema, limit),
            ).fetchall()

        data_directory = None
        try:
            # Run in a separate transaction because permission failures here
            # would abort the transaction for all subsequent queries.
            with self._connect() as conn:
                data_directory = conn.execute("SHOW data_directory").scalar()
        except Exception:
            # Some managed roles cannot read this setting (requires pg_read_all_settings).
            # Keep the diagnostics endpoint usable without elevated grants.
            data_directory = None

        tables = [
            {
                "table_name": str(row.get("table_name") or ""),
                "estimated_rows": int(row.get("estimated_rows") or 0),
                "total_bytes": int(row.get("total_bytes") or 0),
            }
            for row in rows
        ]

        return {
            "database_name": str(database_name or ""),
            "database_size_bytes": int(database_size_bytes or 0),
            "data_directory": str(data_directory or ""),
            "schema": target_schema,
            "tables": tables,
        }


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
                    SET status = ?,
                        finished_at = ?,
                        heartbeat_at = ?,
                        updated_at = ?,
                        error_text = COALESCE(
                            error_text,
                            'Recovered after Manzara restart; previous process state is unknown.'
                        )
                    WHERE status IN ({placeholders})
                    """,
                    (TASK_RUN_STATUS_FAILED, now, now, now, *ACTIVE_STATUSES),
                )
                return int(cur.rowcount or 0)
