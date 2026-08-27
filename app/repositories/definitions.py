from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from app.repositories.core import utc_now


class DefinitionsRepository:
    """PostgreSQL operations for the definitions domain."""

    def prune_runtime_definitions(
        self,
        *,
        panel_ids: Sequence[str],
        task_ids: Sequence[str],
    ) -> Dict[str, int]:
        """Remove stale panel/task definitions and dependent runtime rows."""
        keep_panels = self._normalize_id_list(panel_ids)
        keep_tasks = self._normalize_id_list(task_ids)

        stats = {
            "panels_removed": 0,
            "tasks_removed": 0,
            "runs_removed": 0,
            "events_removed": 0,
        }

        with self._lock:
            with self._connect() as conn:
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
                            meaningful_result_json, gemini_workers_default,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(task_id) DO UPDATE SET
                            panel_id=excluded.panel_id,
                            task_type=excluded.task_type,
                            icon_idle=excluded.icon_idle,
                            icon_running=excluded.icon_running,
                            command_json=excluded.command_json,
                            cwd=excluded.cwd,
                            meaningful_result_json=excluded.meaningful_result_json,
                            gemini_workers_default=excluded.gemini_workers_default,
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
                            item.get("gemini_workers_default"),
                            now,
                            now,
                        ),
                    )

    def set_task_gemini_workers_next(
        self, task_id: str, workers: int
    ) -> Optional[Dict[str, Any]]:
        """Set the one-shot worker override for a supported task."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE task_definitions
                    SET gemini_workers_next = ?, updated_at = ?
                    WHERE task_id = ? AND gemini_workers_default IS NOT NULL
                    """,
                    (workers, now, task_id),
                )
                if int(cur.rowcount or 0) == 0:
                    return None
        return self.get_task(task_id)


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
