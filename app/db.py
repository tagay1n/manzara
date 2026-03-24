"""PostgreSQL access layer for Manzara."""

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

ACTIVE_STATUSES = (
    "starting",
    "running",
    "stopping_graceful",
    "stopping_force",
)

ACTIVE_WORKFLOW_STATUSES = (
    "starting",
    "running",
)


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


class Database:
    """Thin repository layer over PostgreSQL with thread-safe writes."""

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
        repo_root = Path(__file__).resolve().parent.parent
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
        return payload

    def _row_to_workflow(self, row: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(row)
        payload["enabled"] = bool(payload.get("enabled", 0))
        return payload

    def _row_to_step(self, row: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(row)
        payload["condition"] = json.loads(payload.pop("condition_json") or "{}")
        return payload

    def _row_to_schedule(self, row: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(row)
        payload["enabled"] = bool(payload.get("enabled", 0))
        return payload

    def _row_to_workflow_run(self, row: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(row)
        payload["context"] = json.loads(payload.pop("context_json") or "{}")
        return payload

    def _row_to_workflow_step_run(self, row: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(row)
        payload["output"] = json.loads(payload.pop("output_json") or "{}")
        return payload

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
                        "starting",
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
                        now if status not in {"starting", "running"} else None,
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
        return dict(row) if row else None

    def create_run(self, task: Dict[str, Any]) -> int:
        """Create a new run in starting state and return run id."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO runs (
                        task_id, panel_id, status, stop_mode,
                        started_at, heartbeat_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task["task_id"],
                        task["panel_id"],
                        "starting",
                        None,
                        now,
                        now,
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
                    SET status = ?, pid = ?, heartbeat_at = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    ("running", pid, now, now, run_id),
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

    def set_stop_mode(self, run_id: int, mode: str) -> bool:
        """Move an active run into graceful or force stopping mode."""
        status = "stopping_graceful" if mode == "graceful" else "stopping_force"
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

    def list_recent_runs_for_task(self, task_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Return recent runs for one task."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, task_id, panel_id, status, stop_mode,
                       started_at, finished_at, heartbeat_at,
                       pid, exit_code, error_text
                FROM runs
                WHERE task_id = ?
                ORDER BY run_id DESC
                LIMIT ?
                """,
                (task_id, limit),
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
                        updated_at = ?,
                        error_text = COALESCE(
                            error_text,
                            'Recovered after Manzara restart; previous process state is unknown.'
                        )
                    WHERE status IN ({placeholders})
                    """,
                    (now, now, now, *ACTIVE_STATUSES),
                )
                return int(cur.rowcount or 0)

    def recover_active_workflow_runs(self) -> int:
        """Mark previously active workflow runs as failed after process restart."""
        now = utc_now()
        placeholders = ", ".join("?" for _ in ACTIVE_WORKFLOW_STATUSES)
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    f"""
                    UPDATE workflow_runs
                    SET status = 'failed',
                        finished_at = ?,
                        error_text = COALESCE(
                            error_text,
                            'Recovered after Manzara restart; previous workflow state is unknown.'
                        )
                    WHERE status IN ({placeholders})
                    """,
                    (now, *ACTIVE_WORKFLOW_STATUSES),
                )
                return int(cur.rowcount or 0)

    def list_normalization_canonicals(
        self,
        entity_type: str,
        *,
        include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        """Return canonical entities with linked-alias counters."""
        where = "WHERE c.entity_type = ?"
        params: List[Any] = [entity_type]
        if not include_inactive:
            where += " AND c.status = 'active'"

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    c.canonical_id,
                    c.entity_type,
                    c.display_name,
                    c.normalized_name,
                    c.status,
                    c.merged_into_id,
                    c.notes,
                    c.created_at,
                    c.updated_at,
                    COUNT(a.alias_id) AS linked_aliases
                FROM normalization_canonicals c
                LEFT JOIN normalization_aliases a
                    ON a.canonical_id = c.canonical_id
                   AND a.decision_status = 'linked'
                {where}
                GROUP BY c.canonical_id
                ORDER BY linked_aliases DESC, c.display_name ASC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_normalization_canonical(self, canonical_id: int) -> Optional[Dict[str, Any]]:
        """Return one canonical entity by id."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM normalization_canonicals WHERE canonical_id = ?",
                (canonical_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_normalization_canonical(
        self,
        entity_type: str,
        display_name: str,
        normalized_name: str,
        *,
        notes: str = "",
    ) -> Dict[str, Any]:
        """Create a canonical entity."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO normalization_canonicals (
                        entity_type, display_name, normalized_name, status,
                        merged_into_id, notes, created_at, updated_at
                    ) VALUES (?, ?, ?, 'active', NULL, ?, ?, ?)
                    """,
                    (entity_type, display_name, normalized_name, notes, now, now),
                )
                canonical_id = int(cur.lastrowid)
        canonical = self.get_normalization_canonical(canonical_id)
        if not canonical:
            raise RuntimeError("Failed to create canonical")
        return canonical

    def update_normalization_canonical(
        self,
        canonical_id: int,
        updates: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Patch canonical entity fields."""
        allowed = {"display_name", "normalized_name", "status", "merged_into_id", "notes"}
        fields = [field for field in updates.keys() if field in allowed]
        if not fields:
            return self.get_normalization_canonical(canonical_id)

        assignments: List[str] = []
        values: List[Any] = []
        for field in fields:
            assignments.append(f"{field} = ?")
            values.append(updates[field])
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(canonical_id)

        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    UPDATE normalization_canonicals
                    SET {", ".join(assignments)}
                    WHERE canonical_id = ?
                    """,
                    values,
                )
        return self.get_normalization_canonical(canonical_id)

    def delete_normalization_canonical(self, canonical_id: int) -> None:
        """Delete canonical entity row."""
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM normalization_canonicals WHERE canonical_id = ?",
                    (canonical_id,),
                )

    def count_linked_aliases_for_canonical(self, canonical_id: int) -> int:
        """Return number of linked aliases for canonical entity."""
        with self._connect() as conn:
            value = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM normalization_aliases
                WHERE canonical_id = ? AND decision_status = 'linked'
                """,
                (canonical_id,),
            ).scalar()
        return int(value or 0)

    def restore_normalization_canonical_snapshot(self, snapshot: Optional[Dict[str, Any]]) -> None:
        """Restore canonical row from snapshot, or no-op when snapshot is None."""
        if snapshot is None:
            return
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO normalization_canonicals (
                        canonical_id, entity_type, display_name, normalized_name,
                        status, merged_into_id, notes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(canonical_id) DO UPDATE SET
                        entity_type=excluded.entity_type,
                        display_name=excluded.display_name,
                        normalized_name=excluded.normalized_name,
                        status=excluded.status,
                        merged_into_id=excluded.merged_into_id,
                        notes=excluded.notes,
                        updated_at=excluded.updated_at
                    """,
                    (
                        snapshot.get("canonical_id"),
                        snapshot.get("entity_type") or "",
                        snapshot.get("display_name") or "",
                        snapshot.get("normalized_name") or "",
                        snapshot.get("status") or "active",
                        snapshot.get("merged_into_id"),
                        snapshot.get("notes"),
                        snapshot.get("created_at") or now,
                        now,
                    ),
                )

    def list_normalization_aliases(self, entity_type: str) -> List[Dict[str, Any]]:
        """Return saved alias resolutions for an entity type."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM normalization_aliases
                WHERE entity_type = ?
                ORDER BY docs_count DESC, mentions_count DESC, raw_name ASC
                """,
                (entity_type,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_normalization_alias(
        self,
        entity_type: str,
        raw_name: str,
    ) -> Optional[Dict[str, Any]]:
        """Return one alias resolution by entity_type + raw_name."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM normalization_aliases
                WHERE entity_type = ? AND raw_name = ?
                LIMIT 1
                """,
                (entity_type, raw_name),
            ).fetchone()
        return dict(row) if row else None

    def get_normalization_alias_by_id(self, alias_id: int) -> Optional[Dict[str, Any]]:
        """Return one alias resolution by id."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM normalization_aliases WHERE alias_id = ?",
                (alias_id,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_normalization_alias(
        self,
        *,
        entity_type: str,
        raw_name: str,
        normalized_name: str,
        script_label: str,
        docs_count: int,
        mentions_count: int,
        marker_count: int,
        decision_status: str,
        canonical_id: Optional[int],
        confidence: Optional[float],
        source: Optional[str],
        reason: Optional[str],
    ) -> Dict[str, Any]:
        """Insert or update one alias resolution row."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO normalization_aliases (
                        entity_type, raw_name, normalized_name, script_label,
                        docs_count, mentions_count, marker_count,
                        decision_status, canonical_id, confidence, source, reason,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(entity_type, raw_name) DO UPDATE SET
                        normalized_name=excluded.normalized_name,
                        script_label=excluded.script_label,
                        docs_count=excluded.docs_count,
                        mentions_count=excluded.mentions_count,
                        marker_count=excluded.marker_count,
                        decision_status=excluded.decision_status,
                        canonical_id=excluded.canonical_id,
                        confidence=excluded.confidence,
                        source=excluded.source,
                        reason=excluded.reason,
                        updated_at=excluded.updated_at
                    """,
                    (
                        entity_type,
                        raw_name,
                        normalized_name,
                        script_label,
                        int(docs_count),
                        int(mentions_count),
                        int(marker_count),
                        decision_status,
                        canonical_id,
                        confidence,
                        source,
                        reason,
                        now,
                        now,
                    ),
                )
        alias = self.get_normalization_alias(entity_type, raw_name)
        if not alias:
            raise RuntimeError("Failed to upsert normalization alias")
        return alias

    def restore_normalization_alias_snapshot(self, snapshot: Optional[Dict[str, Any]]) -> None:
        """Restore alias row from snapshot, or remove it when snapshot is None."""
        if snapshot is None:
            return
        entity_type = str(snapshot.get("entity_type") or "")
        raw_name = str(snapshot.get("raw_name") or "")
        if not entity_type or not raw_name:
            return
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO normalization_aliases (
                        alias_id, entity_type, raw_name, normalized_name, script_label,
                        docs_count, mentions_count, marker_count,
                        decision_status, canonical_id, confidence, source, reason,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(entity_type, raw_name) DO UPDATE SET
                        alias_id=excluded.alias_id,
                        normalized_name=excluded.normalized_name,
                        script_label=excluded.script_label,
                        docs_count=excluded.docs_count,
                        mentions_count=excluded.mentions_count,
                        marker_count=excluded.marker_count,
                        decision_status=excluded.decision_status,
                        canonical_id=excluded.canonical_id,
                        confidence=excluded.confidence,
                        source=excluded.source,
                        reason=excluded.reason,
                        updated_at=excluded.updated_at
                    """,
                    (
                        snapshot.get("alias_id"),
                        entity_type,
                        raw_name,
                        snapshot.get("normalized_name") or "",
                        snapshot.get("script_label") or "other",
                        int(snapshot.get("docs_count") or 0),
                        int(snapshot.get("mentions_count") or 0),
                        int(snapshot.get("marker_count") or 0),
                        snapshot.get("decision_status") or "pending",
                        snapshot.get("canonical_id"),
                        snapshot.get("confidence"),
                        snapshot.get("source"),
                        snapshot.get("reason"),
                        snapshot.get("created_at") or now,
                        now,
                    ),
                )

    def delete_normalization_alias(self, entity_type: str, raw_name: str) -> None:
        """Delete one alias row."""
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    DELETE FROM normalization_aliases
                    WHERE entity_type = ? AND raw_name = ?
                    """,
                    (entity_type, raw_name),
                )

    def reassign_aliases_between_canonicals(
        self,
        *,
        entity_type: str,
        source_canonical_id: int,
        target_canonical_id: int,
    ) -> List[Dict[str, Any]]:
        """Move linked aliases from source canonical to target canonical."""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM normalization_aliases
                    WHERE entity_type = ?
                      AND canonical_id = ?
                      AND decision_status = 'linked'
                    ORDER BY alias_id ASC
                    """,
                    (entity_type, source_canonical_id),
                ).fetchall()
                snapshots = [dict(row) for row in rows]
                conn.execute(
                    """
                    UPDATE normalization_aliases
                    SET canonical_id = ?, updated_at = ?
                    WHERE entity_type = ?
                      AND canonical_id = ?
                      AND decision_status = 'linked'
                    """,
                    (target_canonical_id, utc_now(), entity_type, source_canonical_id),
                )
        return snapshots

    def create_normalization_event(
        self,
        entity_type: str,
        action: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Persist one normalization audit event row."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO normalization_events (
                        entity_type, action, payload_json, reverted, created_at
                    ) VALUES (?, ?, ?, 0, ?)
                    """,
                    (entity_type, action, json.dumps(payload, ensure_ascii=False), now),
                )
                event_id = int(cur.lastrowid)
        event = self.get_normalization_event(event_id)
        if not event:
            raise RuntimeError("Failed to create normalization event")
        return event

    def get_normalization_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        """Return one normalization event by id."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT event_id, entity_type, action, payload_json, reverted, created_at
                FROM normalization_events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if not row:
            return None
        payload = dict(row)
        payload["reverted"] = bool(payload.get("reverted", 0))
        payload["payload"] = json.loads(payload.pop("payload_json") or "{}")
        return payload

    def list_normalization_events(
        self,
        entity_type: str,
        *,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Return recent normalization events."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, entity_type, action, payload_json, reverted, created_at
                FROM normalization_events
                WHERE entity_type = ?
                ORDER BY event_id DESC
                LIMIT ?
                """,
                (entity_type, max(1, int(limit))),
            ).fetchall()
        items: List[Dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["reverted"] = bool(payload.get("reverted", 0))
            payload["payload"] = json.loads(payload.pop("payload_json") or "{}")
            items.append(payload)
        return items

    def mark_normalization_event_reverted(self, event_id: int) -> None:
        """Mark normalization event as reverted."""
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE normalization_events
                    SET reverted = 1
                    WHERE event_id = ?
                    """,
                    (event_id,),
                )

    def replace_open_suggestions(
        self,
        entity_type: str,
        suggestions: List[Dict[str, Any]],
    ) -> None:
        """Supersede previous open suggestions and insert a fresh set."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE normalization_suggestions
                    SET status = 'superseded', updated_at = ?
                    WHERE entity_type = ? AND status = 'open'
                    """,
                    (now, entity_type),
                )
                for item in suggestions:
                    conn.execute(
                        """
                        INSERT INTO normalization_suggestions (
                            entity_type, raw_name, normalized_name, target_canonical_id,
                            suggestion_kind, confidence, confidence_band,
                            model, rationale, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                        """,
                        (
                            entity_type,
                            item.get("raw_name") or "",
                            item.get("normalized_name") or "",
                            item.get("target_canonical_id"),
                            item.get("suggestion_kind") or "create",
                            float(item.get("confidence") or 0.0),
                            item.get("confidence_band") or "low",
                            item.get("model"),
                            item.get("rationale"),
                            now,
                            now,
                        ),
                    )

    def list_open_suggestions(
        self,
        entity_type: str,
        *,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Return open suggestions for entity type."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM normalization_suggestions
                WHERE entity_type = ? AND status = 'open'
                ORDER BY confidence DESC, suggestion_id DESC
                LIMIT ?
                """,
                (entity_type, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_suggestion_statuses(
        self,
        suggestion_ids: List[int],
        status: str,
    ) -> None:
        """Bulk update suggestion statuses."""
        ids = [int(item) for item in suggestion_ids if int(item) > 0]
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    UPDATE normalization_suggestions
                    SET status = ?, updated_at = ?
                    WHERE suggestion_id IN ({placeholders})
                    """,
                    (status, utc_now(), *ids),
                )
