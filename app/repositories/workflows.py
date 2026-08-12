from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from app.repositories.core import _json_hash, _normalize_shayan_entries, utc_now
from app.runtime_states import (
    TASK_RUN_ACTIVE_STATUSES as ACTIVE_STATUSES,
    TASK_RUN_STATUS_FAILED,
    TASK_RUN_STATUS_RUNNING,
    TASK_RUN_STATUS_STARTING,
    WORKFLOW_RUN_ACTIVE_STATUSES as ACTIVE_WORKFLOW_STATUSES,
    WORKFLOW_RUN_STATUS_FAILED,
    WORKFLOW_RUN_STATUS_RUNNING,
    WORKFLOW_RUN_STATUS_STARTING,
    task_status_from_stop_mode,
)

class WorkflowRepository:
    """PostgreSQL operations for the workflows domain."""

    def create_workflow_run(
        self,
        workflow_id: str,
        schedule_id: Optional[str],
        trigger_source: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Create a workflow run and return run id."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO workflow_runs (
                        workflow_id, schedule_id, trigger_source, status,
                        started_at, context_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workflow_id,
                        schedule_id,
                        trigger_source,
                        WORKFLOW_RUN_STATUS_STARTING,
                        now,
                        json.dumps(context or {}, ensure_ascii=False),
                    ),
                )
                return int(cur.lastrowid)


    def update_workflow_run(
        self,
        workflow_run_id: int,
        *,
        status: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        error_text: Optional[str] = None,
        finished: bool = False,
    ) -> None:
        """Update workflow run fields and optionally finalize it."""
        fields: List[str] = []
        values: List[Any] = []

        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if context is not None:
            fields.append("context_json = ?")
            values.append(json.dumps(context, ensure_ascii=False))
        if error_text is not None or finished:
            fields.append("error_text = ?")
            values.append(error_text)
        if finished:
            fields.append("finished_at = ?")
            values.append(utc_now())

        if not fields:
            return

        values.append(workflow_run_id)
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    UPDATE workflow_runs
                    SET {", ".join(fields)}
                    WHERE workflow_run_id = ?
                    """,
                    values,
                )


    def get_workflow_run(self, workflow_run_id: int) -> Optional[Dict[str, Any]]:
        """Return one workflow run by id."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_runs WHERE workflow_run_id = ?",
                (workflow_run_id,),
            ).fetchone()
        return self._row_to_workflow_run(row) if row else None


    def get_active_workflow_run(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """Return active workflow run for workflow, if any."""
        placeholders = ", ".join("?" for _ in ACTIVE_WORKFLOW_STATUSES)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT *
                FROM workflow_runs
                WHERE workflow_id = ?
                  AND status IN ({placeholders})
                ORDER BY workflow_run_id DESC
                LIMIT 1
                """,
                (workflow_id, *ACTIVE_WORKFLOW_STATUSES),
            ).fetchone()
        return self._row_to_workflow_run(row) if row else None


    def list_recent_workflow_runs(self, workflow_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Return recent workflow runs for one workflow."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM workflow_runs
                WHERE workflow_id = ?
                ORDER BY workflow_run_id DESC
                LIMIT ?
                """,
                (workflow_id, limit),
            ).fetchall()
        return [self._row_to_workflow_run(row) for row in rows]


    def create_workflow_step_run(
        self,
        workflow_run_id: int,
        step_order: int,
        task_id: Optional[str],
        status: str,
        *,
        task_run_id: Optional[int] = None,
        output: Optional[Dict[str, Any]] = None,
        error_text: Optional[str] = None,
    ) -> int:
        """Insert one workflow step run row and return step run id."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO workflow_step_runs (
                        workflow_run_id, step_order, task_id, status,
                        task_run_id, started_at, finished_at,
                        output_json, error_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workflow_run_id,
                        step_order,
                        task_id,
                        status,
                        task_run_id,
                        now,
                        now
                        if status
                        not in {WORKFLOW_RUN_STATUS_STARTING, WORKFLOW_RUN_STATUS_RUNNING}
                        else None,
                        json.dumps(output or {}, ensure_ascii=False),
                        error_text,
                    ),
                )
                return int(cur.lastrowid)


    def finish_workflow_step_run(
        self,
        step_run_id: int,
        *,
        status: str,
        output: Optional[Dict[str, Any]] = None,
        error_text: Optional[str] = None,
    ) -> None:
        """Finalize one step run."""
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE workflow_step_runs
                    SET status = ?,
                        output_json = ?,
                        error_text = ?,
                        finished_at = ?
                    WHERE step_run_id = ?
                    """,
                    (
                        status,
                        json.dumps(output or {}, ensure_ascii=False),
                        error_text,
                        utc_now(),
                        step_run_id,
                    ),
                )


    def list_workflow_step_runs(self, workflow_run_id: int) -> List[Dict[str, Any]]:
        """Return all step runs for one workflow run."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM workflow_step_runs
                WHERE workflow_run_id = ?
                ORDER BY step_order ASC, step_run_id ASC
                """,
                (workflow_run_id,),
            ).fetchall()
        return [self._row_to_workflow_step_run(row) for row in rows]
