from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from app.repositories.core import utc_now
from app.runtime_config import config_integer, config_number
from app.runtime_states import (
    TASK_RUN_ACTIVE_STATUSES as ACTIVE_STATUSES,
)
from app.runtime_states import (
    TASK_RUN_STATUS_FAILED,
    TASK_RUN_STATUS_RUNNING,
    TASK_RUN_STATUS_STARTING,
    TASK_RUN_STATUS_STOPPING_GRACEFUL,
)


class RunRepository:
    """Machine-local run history, progress, and provider-wait snapshots."""

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


    def create_run(self, *, task_id: str, panel_id: str) -> int:
        """Create a run from the handler registration and explicit options."""
        now = utc_now()
        with self._lock:
            with self._runtime_connect(immediate=True) as conn:
                cur = conn.execute(
                    """
                    INSERT INTO runs (
                        task_id, panel_id, status, stop_mode,
                        started_at, heartbeat_at, created_at, updated_at, summary_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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


    def publish_run_progress(
        self,
        *,
        run_id: int,
        progress: Dict[str, Any],
        force: bool = False,
        minimum_interval_seconds: float | None = None,
    ) -> bool:
        """Persist the latest coalesced progress snapshot without event duplication."""
        if not isinstance(progress, dict):
            raise ValueError("progress must be an object")
        if minimum_interval_seconds is None:
            minimum_interval_seconds = config_number("runtime", "progress_interval_seconds")
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


    def set_run_provider_wait(self, run_id: int, wait: Dict[str, Any]) -> None:
        """Replace transient provider status independently of item progress."""
        with self._lock:
            with self._runtime_connect() as conn:
                conn.execute(
                    "UPDATE runs SET provider_wait_json = ?, updated_at = ? WHERE run_id = ?",
                    (json.dumps(wait, ensure_ascii=False), utc_now(), run_id),
                )


    def request_run_stop(self, run_id: int) -> bool:
        """Request the only checkpointed stop mode: graceful."""
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
                    ("graceful", TASK_RUN_STATUS_STOPPING_GRACEFUL, now, now, run_id, *ACTIVE_STATUSES),
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
                    SET status = ?, exit_code = ?, error_text = ?, provider_wait_json = '{}',
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


    def get_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        """Return one run by id."""
        with self._runtime_connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None


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


    def list_recent_runs_for_task(self, task_id: str, limit: int | None = None) -> List[Dict[str, Any]]:
        """Return recent runs for one task."""
        if limit is None:
            limit = config_integer("runtime", "list_page_size")
        with self._runtime_connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, task_id, panel_id, status, stop_mode,
                       started_at, finished_at, heartbeat_at,
                       pid, exit_code, error_text, summary_json, progress_json, provider_wait_json
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
