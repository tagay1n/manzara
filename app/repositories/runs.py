from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from app.repositories.core import utc_now
from app.runtime_states import (
    TASK_RUN_ACTIVE_STATUSES as ACTIVE_STATUSES,
)
from app.runtime_states import (
    TASK_RUN_STATUS_FAILED,
    TASK_RUN_STATUS_RUNNING,
    TASK_RUN_STATUS_STARTING,
    task_status_from_stop_mode,
)


class RunRepository:
    """Machine-local run history, progress snapshots, and structured events."""

    def get_latest_run_for_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Return most recent run for task."""
        with self._runtime_connect() as conn:
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


    def create_run(self, *, task_id: str, panel_id: str, workers: int) -> int:
        """Create a run from the handler registration and explicit options."""
        now = utc_now()
        with self._lock:
            with self._runtime_connect(immediate=True) as conn:
                cur = conn.execute(
                    """
                    INSERT INTO runs (
                        task_id, panel_id, status, stop_mode,
                        started_at, heartbeat_at, created_at, updated_at, summary_json,
                        gemini_workers
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        panel_id,
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
            with self._runtime_connect() as conn:
                conn.execute(
                    """
                    UPDATE runs
                    SET status = ?, pid = ?, heartbeat_at = ?, updated_at = ?
                    WHERE run_id = ? AND status = 'starting'
                    """,
                    (TASK_RUN_STATUS_RUNNING, pid, now, now, run_id),
                )


    def heartbeat(self, run_id: int) -> None:
        """Update run heartbeat timestamp."""
        now = utc_now()
        with self._lock:
            with self._runtime_connect() as conn:
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
            with self._runtime_connect() as conn:
                conn.execute(
                    """
                    UPDATE runs
                    SET progress_json = ?, heartbeat_at = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (json.dumps(progress, ensure_ascii=False), now, now, int(run_id)),
                )


    def publish_run_progress(
        self,
        *,
        run_id: int,
        progress: Dict[str, Any],
        force: bool = False,
        minimum_interval_seconds: float = 1.0,
    ) -> bool:
        """Persist the latest coalesced progress snapshot without event duplication."""
        if not isinstance(progress, dict):
            raise ValueError("progress must be an object")
        resolved_run_id = int(run_id)
        now_monotonic = time.monotonic()
        with self._lock:
            last = float(self._progress_last_published.get(resolved_run_id, 0.0))
            if not force and now_monotonic - last < max(0.0, minimum_interval_seconds):
                return False

            timestamp = utc_now()
            with self._runtime_connect() as conn:
                conn.execute(
                    """
                    UPDATE runs
                    SET progress_json = ?, heartbeat_at = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        json.dumps(progress, ensure_ascii=False),
                        timestamp,
                        timestamp,
                        resolved_run_id,
                    ),
                )
            self._progress_last_published[resolved_run_id] = now_monotonic
        return True


    def set_stop_mode(self, run_id: int, mode: str) -> bool:
        """Move an active run into graceful or force stopping mode."""
        status = task_status_from_stop_mode(mode)
        now = utc_now()
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        with self._lock:
            with self._runtime_connect() as conn:
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
            with self._runtime_connect() as conn:
                conn.execute(
                    """
                    UPDATE runs
                    SET status = ?, exit_code = ?, error_text = ?,
                        finished_at = ?, heartbeat_at = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (status, exit_code, error_text, now, now, now, run_id),
                )
            self._progress_last_published.pop(int(run_id), None)


    def update_run_summary(self, run_id: int, summary: Dict[str, Any]) -> None:
        """Persist structured summary payload for one run."""
        with self._lock:
            with self._runtime_connect() as conn:
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
            with self._runtime_connect() as conn:
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
        with self._runtime_connect() as conn:
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
        with self._runtime_connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(event_id), 0) AS event_id FROM events"
            ).fetchone()
        return int(row["event_id"] or 0) if row else 0


    def get_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        """Return one run by id."""
        with self._runtime_connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None


    def list_active_runs(self) -> List[Dict[str, Any]]:
        """Return active runs across all tasks."""
        with self._runtime_connect() as conn:
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
        with self._runtime_connect() as conn:
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


    def list_recent_runs_for_task(self, task_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Return recent runs for one task."""
        with self._runtime_connect() as conn:
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


    def recover_active_runs(self) -> int:
        """Mark previously active runs as failed after process restart."""
        now = utc_now()
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        with self._lock:
            with self._runtime_connect() as conn:
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
