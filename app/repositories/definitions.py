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

class DefinitionsRepository:
    """PostgreSQL operations for the definitions domain."""

    def prune_runtime_definitions(
        self,
        *,
        panel_ids: Sequence[str],
        task_ids: Sequence[str],
        workflow_ids: Sequence[str],
    ) -> Dict[str, int]:
        """Remove stale panel/task/workflow definitions and dependent runtime rows."""
        keep_panels = self._normalize_id_list(panel_ids)
        keep_tasks = self._normalize_id_list(task_ids)
        keep_workflows = self._normalize_id_list(workflow_ids)

        stats = {
            "panels_removed": 0,
            "tasks_removed": 0,
            "workflows_removed": 0,
            "workflow_runs_removed": 0,
            "runs_removed": 0,
            "events_removed": 0,
            "workflow_step_runs_removed": 0,
        }

        with self._lock:
            with self._connect() as conn:
                obsolete_workflow_ids = self._select_obsolete_ids(
                    conn,
                    table="workflows",
                    id_column="workflow_id",
                    keep_ids=keep_workflows,
                )
                obsolete_task_ids = self._select_obsolete_ids(
                    conn,
                    table="task_definitions",
                    id_column="task_id",
                    keep_ids=keep_tasks,
                )
                obsolete_panel_ids = self._select_obsolete_ids(
                    conn,
                    table="panel_definitions",
                    id_column="panel_id",
                    keep_ids=keep_panels,
                )

                obsolete_schedule_ids: List[str] = []
                obsolete_workflow_run_ids: List[int] = []
                if obsolete_workflow_ids:
                    wf_rows = conn.execute(
                        "SELECT schedule_id FROM workflow_schedules WHERE workflow_id IN "
                        f"({self._placeholders(len(obsolete_workflow_ids))})",
                        obsolete_workflow_ids,
                    ).fetchall()
                    obsolete_schedule_ids = [
                        str(row["schedule_id"])
                        for row in wf_rows
                        if str(row.get("schedule_id") or "").strip()
                    ]

                    run_rows = conn.execute(
                        "SELECT workflow_run_id FROM workflow_runs WHERE workflow_id IN "
                        f"({self._placeholders(len(obsolete_workflow_ids))})",
                        obsolete_workflow_ids,
                    ).fetchall()
                    obsolete_workflow_run_ids.extend(
                        int(row["workflow_run_id"])
                        for row in run_rows
                        if row.get("workflow_run_id") is not None
                    )
                    if obsolete_schedule_ids:
                        schedule_run_rows = conn.execute(
                            "SELECT workflow_run_id FROM workflow_runs WHERE schedule_id IN "
                            f"({self._placeholders(len(obsolete_schedule_ids))})",
                            obsolete_schedule_ids,
                        ).fetchall()
                        obsolete_workflow_run_ids.extend(
                            int(row["workflow_run_id"])
                            for row in schedule_run_rows
                            if row.get("workflow_run_id") is not None
                        )
                    obsolete_workflow_run_ids = sorted(set(obsolete_workflow_run_ids))

                if obsolete_workflow_run_ids:
                    stats["workflow_step_runs_removed"] += self._delete_in(
                        conn,
                        table="workflow_step_runs",
                        id_column="workflow_run_id",
                        values=obsolete_workflow_run_ids,
                    )
                    stats["workflow_runs_removed"] += self._delete_in(
                        conn,
                        table="workflow_runs",
                        id_column="workflow_run_id",
                        values=obsolete_workflow_run_ids,
                    )

                if obsolete_workflow_ids:
                    stats["workflows_removed"] += self._delete_in(
                        conn,
                        table="workflow_steps",
                        id_column="workflow_id",
                        values=obsolete_workflow_ids,
                    )
                    _ = self._delete_in(
                        conn,
                        table="workflow_schedules",
                        id_column="workflow_id",
                        values=obsolete_workflow_ids,
                    )
                    stats["workflows_removed"] += self._delete_in(
                        conn,
                        table="workflows",
                        id_column="workflow_id",
                        values=obsolete_workflow_ids,
                    )

                obsolete_run_ids: List[int] = []
                if obsolete_task_ids:
                    run_rows = conn.execute(
                        "SELECT run_id FROM runs WHERE task_id IN "
                        f"({self._placeholders(len(obsolete_task_ids))})",
                        obsolete_task_ids,
                    ).fetchall()
                    obsolete_run_ids = [
                        int(row["run_id"])
                        for row in run_rows
                        if row.get("run_id") is not None
                    ]
                    if obsolete_run_ids:
                        stats["workflow_step_runs_removed"] += self._delete_in(
                            conn,
                            table="workflow_step_runs",
                            id_column="task_run_id",
                            values=obsolete_run_ids,
                        )
                        stats["events_removed"] += self._delete_in(
                            conn,
                            table="events",
                            id_column="run_id",
                            values=obsolete_run_ids,
                        )
                    stats["events_removed"] += self._delete_in(
                        conn,
                        table="events",
                        id_column="task_id",
                        values=obsolete_task_ids,
                    )
                    if obsolete_run_ids:
                        stats["runs_removed"] += self._delete_in(
                            conn,
                            table="runs",
                            id_column="run_id",
                            values=obsolete_run_ids,
                        )
                    stats["tasks_removed"] += self._delete_in(
                        conn,
                        table="task_definitions",
                        id_column="task_id",
                        values=obsolete_task_ids,
                    )

                if obsolete_panel_ids:
                    stats["events_removed"] += self._delete_in(
                        conn,
                        table="events",
                        id_column="panel_id",
                        values=obsolete_panel_ids,
                    )
                    stats["panels_removed"] += self._delete_in(
                        conn,
                        table="panel_definitions",
                        id_column="panel_id",
                        values=obsolete_panel_ids,
                    )

        return stats


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
                            meaningful_result_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(task_id) DO UPDATE SET
                            panel_id=excluded.panel_id,
                            task_type=excluded.task_type,
                            icon_idle=excluded.icon_idle,
                            icon_running=excluded.icon_running,
                            command_json=excluded.command_json,
                            cwd=excluded.cwd,
                            meaningful_result_json=excluded.meaningful_result_json,
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
                            json.dumps(item.get("meaningful_result", {}), ensure_ascii=False),
                            now,
                            now,
                        ),
                    )


    def seed_panels(self, panel_defs: List[Dict[str, Any]]) -> None:
        """Insert panel definitions when missing; preserve user-renamed titles."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                for item in panel_defs:
                    conn.execute(
                        """
                        INSERT INTO panel_definitions (
                            panel_id, title, created_at, updated_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(panel_id) DO NOTHING
                        """,
                        (
                            item["panel_id"],
                            item["title"],
                            now,
                            now,
                        ),
                    )


    def seed_workflow_bundle(self, bundle: Dict[str, Any]) -> None:
        """Seed a workflow definition and default schedule."""
        workflow = bundle["workflow"]
        steps = bundle.get("steps", [])
        schedule = bundle.get("schedule")
        now = utc_now()

        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO workflows (
                        workflow_id, panel_id, title, description,
                        enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workflow_id) DO UPDATE SET
                        panel_id=excluded.panel_id,
                        title=excluded.title,
                        description=excluded.description,
                        enabled=excluded.enabled,
                        updated_at=excluded.updated_at
                    """,
                    (
                        workflow["workflow_id"],
                        workflow["panel_id"],
                        workflow["title"],
                        workflow.get("description", ""),
                        int(workflow.get("enabled", 1)),
                        now,
                        now,
                    ),
                )

                conn.execute(
                    "DELETE FROM workflow_steps WHERE workflow_id = ?",
                    (workflow["workflow_id"],),
                )
                for step in steps:
                    conn.execute(
                        """
                        INSERT INTO workflow_steps (
                            workflow_id, step_order, step_type, task_id,
                            condition_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            workflow["workflow_id"],
                            int(step["step_order"]),
                            step.get("step_type", "task"),
                            step.get("task_id"),
                            json.dumps(step.get("condition_json", {}), ensure_ascii=False),
                            now,
                            now,
                        ),
                    )

                if schedule:
                    conn.execute(
                        """
                        INSERT INTO workflow_schedules (
                            schedule_id, workflow_id, schedule_type,
                            day_of_week, time_of_day, timezone, interval_minutes,
                            enabled, overlap_policy, catchup_policy,
                            next_run_at, last_run_at,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(schedule_id) DO NOTHING
                        """,
                        (
                            schedule["schedule_id"],
                            schedule["workflow_id"],
                            schedule.get("schedule_type", "weekly"),
                            int(schedule.get("day_of_week", 1)),
                            schedule.get("time_of_day", "03:00"),
                            schedule.get("timezone", "UTC"),
                            (
                                int(schedule.get("interval_minutes"))
                                if schedule.get("interval_minutes") is not None
                                else None
                            ),
                            int(schedule.get("enabled", 0)),
                            schedule.get("overlap_policy", "skip"),
                            schedule.get("catchup_policy", "once"),
                            schedule.get("next_run_at"),
                            schedule.get("last_run_at"),
                            now,
                            now,
                        ),
                )


    def get_panel(self, panel_id: str) -> Optional[Dict[str, Any]]:
        """Return one panel definition by id."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM panel_definitions WHERE panel_id = ?",
                (panel_id,),
            ).fetchone()
        return dict(row) if row else None


    def get_panel_title_map(self) -> Dict[str, str]:
        """Return mapping of panel id to display title."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT panel_id, title FROM panel_definitions ORDER BY panel_id"
            ).fetchall()
        return {str(row["panel_id"]): str(row["title"]) for row in rows}


    def update_panel_title(self, panel_id: str, title: str) -> Optional[Dict[str, Any]]:
        """Update one panel title and return updated panel row."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE panel_definitions
                    SET title = ?, updated_at = ?
                    WHERE panel_id = ?
                    """,
                    (title, now, panel_id),
                )
                if int(cur.rowcount or 0) == 0:
                    return None
        return self.get_panel(panel_id)


    def update_task_title(self, task_id: str, title: str) -> Optional[Dict[str, Any]]:
        """Update one task title and return updated task definition."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE task_definitions
                    SET title = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (title, now, task_id),
                )
                if int(cur.rowcount or 0) == 0:
                    return None
        return self.get_task(task_id)


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


    def get_workflow(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """Return one workflow definition."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflows WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
        return self._row_to_workflow(row) if row else None


    def list_workflow_steps(self, workflow_id: str) -> List[Dict[str, Any]]:
        """Return ordered step definitions for one workflow."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM workflow_steps
                WHERE workflow_id = ?
                ORDER BY step_order ASC
                """,
                (workflow_id,),
            ).fetchall()
        return [self._row_to_step(row) for row in rows]


    def list_workflows_with_latest_run(
        self,
        panel_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return workflows with latest workflow run and schedule details."""
        where_clause = ""
        params: List[Any] = []
        if panel_id:
            where_clause = "WHERE w.panel_id = ?"
            params.append(panel_id)

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    w.workflow_id,
                    w.panel_id,
                    w.title,
                    w.description,
                    w.enabled,
                    wr.workflow_run_id,
                    wr.status AS run_status,
                    wr.trigger_source,
                    wr.started_at,
                    wr.finished_at,
                    wr.error_text,
                    s.schedule_id,
                    s.schedule_type,
                    s.day_of_week,
                    s.time_of_day,
                    s.timezone,
                    s.interval_minutes,
                    s.enabled AS schedule_enabled,
                    s.overlap_policy,
                    s.catchup_policy,
                    s.next_run_at,
                    s.last_run_at
                FROM workflows w
                LEFT JOIN workflow_runs wr
                    ON wr.workflow_run_id = (
                        SELECT workflow_run_id
                        FROM workflow_runs wr2
                        WHERE wr2.workflow_id = w.workflow_id
                        ORDER BY wr2.workflow_run_id DESC
                        LIMIT 1
                    )
                LEFT JOIN workflow_schedules s
                    ON s.workflow_id = w.workflow_id
                {where_clause}
                ORDER BY w.panel_id, w.title
                """,
                params,
            ).fetchall()

        payloads: List[Dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["enabled"] = bool(payload.get("enabled", 0))
            if payload.get("schedule_id"):
                payload["schedule_enabled"] = bool(payload.get("schedule_enabled", 0))
            payloads.append(payload)
        return payloads


    def get_schedule(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        """Return schedule row by id."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_schedules WHERE schedule_id = ?",
                (schedule_id,),
            ).fetchone()
        return self._row_to_schedule(row) if row else None


    def get_schedule_by_workflow(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """Return schedule for workflow if present."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM workflow_schedules
                WHERE workflow_id = ?
                LIMIT 1
                """,
                (workflow_id,),
            ).fetchone()
        return self._row_to_schedule(row) if row else None


    def list_enabled_schedules(self) -> List[Dict[str, Any]]:
        """Return all enabled schedules."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM workflow_schedules
                WHERE enabled = 1
                ORDER BY schedule_id
                """
            ).fetchall()
        return [self._row_to_schedule(row) for row in rows]


    def list_due_schedules(self, now_iso: str) -> List[Dict[str, Any]]:
        """Return schedules that are due at or before provided UTC timestamp."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM workflow_schedules
                WHERE enabled = 1
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= ?
                ORDER BY next_run_at ASC
                """,
                (now_iso,),
            ).fetchall()
        return [self._row_to_schedule(row) for row in rows]


    def update_schedule(
        self,
        schedule_id: str,
        updates: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Patch a schedule row and return updated row."""
        allowed = {
            "enabled",
            "schedule_type",
            "day_of_week",
            "time_of_day",
            "timezone",
            "interval_minutes",
            "overlap_policy",
            "catchup_policy",
            "next_run_at",
            "last_run_at",
        }
        fields = [field for field in updates.keys() if field in allowed]
        if not fields:
            return self.get_schedule(schedule_id)

        values: List[Any] = []
        assignments: List[str] = []
        for field in fields:
            value = updates[field]
            if field == "enabled" and isinstance(value, bool):
                value = int(value)
            if field == "interval_minutes" and value is not None:
                value = int(value)
            assignments.append(f"{field} = ?")
            values.append(value)

        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(schedule_id)

        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    UPDATE workflow_schedules
                    SET {", ".join(assignments)}
                    WHERE schedule_id = ?
                    """,
                    values,
                )
        return self.get_schedule(schedule_id)
