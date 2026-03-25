"""Shared API payload composition helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional

from fastapi import HTTPException

from app.contracts import PayloadBuilderOperations


class PayloadBuilder:
    """Compose API payloads from DB/runtime state using injected operations."""

    def __init__(
        self,
        *,
        state_provider: Callable[[], Any],
        panel_defs_provider: Callable[[], list[Dict[str, Any]]],
        normalization_entity_types: Iterable[str],
        slug_separator_pattern: re.Pattern[str],
        slug_clean_pattern: re.Pattern[str],
        ops_provider: Callable[[], PayloadBuilderOperations],
    ) -> None:
        self._state_provider = state_provider
        self._panel_defs_provider = panel_defs_provider
        self._normalization_entity_types = {str(item) for item in normalization_entity_types}
        self._slug_separator_pattern = slug_separator_pattern
        self._slug_clean_pattern = slug_clean_pattern
        self._ops_provider = ops_provider

    def _state(self) -> Any:
        return self._state_provider()

    def _ops(self) -> PayloadBuilderOperations:
        return self._ops_provider()

    def _slugify(self, value: Any) -> str:
        text = str(value or "").strip().lower()
        text = self._slug_separator_pattern.sub("-", text)
        text = self._slug_clean_pattern.sub("-", text)
        text = re.sub(r"-{2,}", "-", text)
        return text.strip("-")

    def _task_slug_maps(self) -> tuple[Dict[str, str], Dict[str, str]]:
        """Return deterministic task_id<->slug maps."""
        state = self._state()
        tasks = sorted(
            state.db.list_tasks(),
            key=lambda item: (
                str(item.get("panel_id") or ""),
                str(item.get("title") or ""),
                str(item.get("task_id") or ""),
            ),
        )
        used: set[str] = set()
        task_to_slug: Dict[str, str] = {}
        slug_to_task: Dict[str, str] = {}

        for task in tasks:
            task_id = str(task["task_id"])
            base = self._slugify(task.get("title")) or self._slugify(task_id) or "task"
            panel_slug = self._slugify(task.get("panel_id")) or "flow"
            candidate = base
            attempt = 1
            while candidate in used:
                if attempt == 1:
                    candidate = f"{base}-{panel_slug}"
                else:
                    candidate = f"{base}-{panel_slug}-{attempt}"
                attempt += 1
            used.add(candidate)
            task_to_slug[task_id] = candidate
            slug_to_task[candidate] = task_id

        return task_to_slug, slug_to_task

    def _resolve_task_identifier(self, task_key: str) -> Dict[str, Any]:
        """Resolve task by id or human slug."""
        state = self._state()
        task = state.db.get_task(task_key)
        if task:
            return task
        _, slug_to_task = self._task_slug_maps()
        task_id = slug_to_task.get(task_key)
        if not task_id:
            raise HTTPException(status_code=404, detail="Task not found")
        resolved = state.db.get_task(task_id)
        if not resolved:
            raise HTTPException(status_code=404, detail="Task not found")
        return resolved

    def _flow_slug_maps(self) -> tuple[Dict[str, str], Dict[str, str]]:
        """Return deterministic panel_id<->slug maps."""
        state = self._state()
        title_map = state.db.get_panel_title_map()
        panel_ids = {str(item["panel_id"]) for item in self._panel_defs_provider()}
        panel_ids.update(title_map.keys())

        used: set[str] = set()
        panel_to_slug: Dict[str, str] = {}
        slug_to_panel: Dict[str, str] = {}
        for panel_id in sorted(panel_ids):
            display_title = str(title_map.get(panel_id, panel_id))
            base = self._slugify(display_title) or self._slugify(panel_id) or "flow"
            candidate = base
            attempt = 1
            while candidate in used:
                if attempt == 1:
                    candidate = f"{base}-{self._slugify(panel_id) or panel_id}"
                else:
                    candidate = f"{base}-{attempt}"
                attempt += 1
            used.add(candidate)
            panel_to_slug[panel_id] = candidate
            slug_to_panel[candidate] = panel_id
        return panel_to_slug, slug_to_panel

    def _resolve_flow_identifier(self, flow_key: str) -> Dict[str, Any]:
        """Resolve flow by panel id or human slug."""
        state = self._state()
        panel = state.db.get_panel(flow_key)
        if panel:
            return panel
        _, slug_to_panel = self._flow_slug_maps()
        panel_id = slug_to_panel.get(flow_key)
        if not panel_id:
            raise HTTPException(status_code=404, detail="Flow not found")
        resolved = state.db.get_panel(panel_id)
        if not resolved:
            raise HTTPException(status_code=404, detail="Flow not found")
        return resolved

    def _run_with_summary(self, run: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(run)
        summary = payload.get("summary")
        if not isinstance(summary, dict) or not summary:
            payload["summary"] = self._ops()["build_default_run_summary"](payload)
        return payload

    def _count_active_workflows(self, workflows: Optional[list[Dict[str, Any]]] = None) -> int:
        state = self._state()
        rows = workflows if workflows is not None else state.db.list_workflows_with_latest_run()
        return len([row for row in rows if row.get("run_status") in {"starting", "running"}])

    @staticmethod
    def _resolve_stop_all_state(active_runs: list[Dict[str, Any]]) -> str:
        if not active_runs:
            return "disabled"
        if any(run.get("stop_mode") is None for run in active_runs):
            return "normal"
        return "armed"

    def _build_global_payload(
        self,
        *,
        active_runs: Optional[list[Dict[str, Any]]] = None,
        active_workflows: Optional[int] = None,
        include_failed_runs: bool = False,
    ) -> Dict[str, Any]:
        state = self._state()
        runs = active_runs if active_runs is not None else state.db.list_active_runs()
        payload: Dict[str, Any] = {
            "active_tasks": len(runs),
            "active_workflows": (
                active_workflows
                if active_workflows is not None
                else self._count_active_workflows()
            ),
            "stop_all_state": self._resolve_stop_all_state(runs),
        }
        if include_failed_runs:
            payload["failed_runs"] = len(
                [run for run in state.db.list_recent_runs(50) if run["status"] == "failed"]
            )
        return payload

    def _build_panel_payloads(
        self,
        *,
        tasks_by_panel: Dict[str, list[Dict[str, Any]]],
        panel_titles: Dict[str, str],
        workflows: list[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        ops = self._ops()
        state = self._state()
        shayan_workflows = [item for item in workflows if item.get("panel_id") == "shayan"]
        shayan_panel = ops["build_shayan_panel"](
            db=state.db,
            shayan=state.settings.shayan,
            tasks=tasks_by_panel.get("shayan", []),
            workflows=shayan_workflows,
            title=panel_titles.get("shayan", "Shayan"),
        )
        maintenance_panel = ops["build_maintenance_panel"](
            db=state.db,
            maintenance=state.settings.maintenance,
            tasks=tasks_by_panel.get("maintenance", []),
            title=panel_titles.get("maintenance", "Maintenance"),
        )
        library_panel = ops["build_library_panel"](
            db=state.db,
            maintenance=state.settings.maintenance,
            tasks=tasks_by_panel.get("library", []),
            title=panel_titles.get("library", "Library"),
        )
        oscar_panel = ops["build_oscar_panel"](
            db=state.db,
            oscar=state.settings.oscar,
            tasks=tasks_by_panel.get("oscar", []),
            title=panel_titles.get("oscar", "Oscar"),
        )
        return {
            "shayan": shayan_panel,
            "maintenance": maintenance_panel,
            "oscar": oscar_panel,
            "library": library_panel,
        }

    def build_dashboard_payload(self) -> Dict[str, Any]:
        """Compose dashboard payload from DB and flow artifacts."""
        state = self._state()
        panel_titles = state.db.get_panel_title_map()
        task_slug_map, _ = self._task_slug_maps()
        flow_slug_map, _ = self._flow_slug_maps()

        tasks = state.db.list_tasks_with_latest_run()
        tasks_by_panel: Dict[str, list[Dict[str, Any]]] = {}
        for task in tasks:
            panel_id = task["panel_id"]
            tasks_by_panel.setdefault(panel_id, []).append(
                {
                    "task_id": task["task_id"],
                    "slug": task_slug_map.get(str(task["task_id"]), str(task["task_id"])),
                    "title": task["title"],
                    "task_type": task["task_type"],
                    "icon_idle": task["icon_idle"],
                    "icon_running": task["icon_running"],
                    "run": {
                        "run_id": task.get("run_id"),
                        "status": task.get("run_status") or "idle",
                        "stop_mode": task.get("stop_mode"),
                        "started_at": task.get("started_at"),
                        "finished_at": task.get("finished_at"),
                        "heartbeat_at": task.get("heartbeat_at"),
                        "exit_code": task.get("exit_code"),
                        "error_text": task.get("error_text"),
                        "summary": (
                            task.get("run_summary")
                            if isinstance(task.get("run_summary"), dict) and task.get("run_summary")
                            else None
                        ),
                    },
                }
            )

        workflows = state.db.list_workflows_with_latest_run()
        panel_payloads = self._build_panel_payloads(
            tasks_by_panel=tasks_by_panel,
            panel_titles=panel_titles,
            workflows=workflows,
        )
        ordered_panels = [
            panel_payloads["shayan"],
            panel_payloads["maintenance"],
            panel_payloads["oscar"],
            panel_payloads["library"],
        ]
        for panel in ordered_panels:
            panel_id = str(panel.get("panel_id") or "")
            panel["slug"] = flow_slug_map.get(panel_id, panel_id)

        active_runs = state.db.list_active_runs()
        active_workflow_runs = self._count_active_workflows(workflows)

        recent_runs = state.db.list_recent_runs(20)
        for run in recent_runs:
            task_id = str(run.get("task_id") or "")
            run["task_slug"] = task_slug_map.get(task_id, task_id)
            run["summary"] = self._run_with_summary(run).get("summary", {})

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "global": self._build_global_payload(
                active_runs=active_runs,
                active_workflows=active_workflow_runs,
                include_failed_runs=True,
            ),
            "panels": ordered_panels,
            "recent_runs": recent_runs,
            "scheduler": {
                "enabled": state.settings.scheduler_enabled,
            },
        }

    def build_schedules_payload(self) -> Dict[str, Any]:
        """Compose schedules page payload from workflow/schedule state."""
        state = self._state()
        workflows = state.db.list_workflows_with_latest_run()
        workflow_items: list[Dict[str, Any]] = []
        for workflow in workflows:
            schedule: Dict[str, Any] | None = None
            if workflow.get("schedule_id"):
                schedule = {
                    "schedule_id": workflow.get("schedule_id"),
                    "schedule_type": workflow.get("schedule_type"),
                    "day_of_week": workflow.get("day_of_week"),
                    "time_of_day": workflow.get("time_of_day"),
                    "timezone": workflow.get("timezone"),
                    "interval_minutes": workflow.get("interval_minutes"),
                    "enabled": bool(workflow.get("schedule_enabled", False)),
                    "overlap_policy": workflow.get("overlap_policy"),
                    "catchup_policy": workflow.get("catchup_policy"),
                    "next_run_at": workflow.get("next_run_at"),
                    "last_run_at": workflow.get("last_run_at"),
                }

            workflow_items.append(
                {
                    "workflow_id": workflow["workflow_id"],
                    "panel_id": workflow["panel_id"],
                    "title": workflow["title"],
                    "description": workflow.get("description") or "",
                    "enabled": bool(workflow.get("enabled", True)),
                    "run": {
                        "workflow_run_id": workflow.get("workflow_run_id"),
                        "status": workflow.get("run_status") or "idle",
                        "trigger_source": workflow.get("trigger_source"),
                        "started_at": workflow.get("started_at"),
                        "finished_at": workflow.get("finished_at"),
                        "error_text": workflow.get("error_text"),
                    },
                    "schedule": schedule,
                }
            )

        active_runs = state.db.list_active_runs()
        active_workflows = len(
            [workflow for workflow in workflow_items if workflow["run"]["status"] in {"starting", "running"}]
        )

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "global": self._build_global_payload(
                active_runs=active_runs,
                active_workflows=active_workflows,
            ),
            "scheduler": {
                "enabled": state.settings.scheduler_enabled,
            },
            "workflows": workflow_items,
        }

    def build_tasks_payload(self) -> Dict[str, Any]:
        """Compose tasks page payload grouped by flow."""
        state = self._state()
        panel_titles = state.db.get_panel_title_map()
        task_slug_map, _ = self._task_slug_maps()
        flow_slug_map, _ = self._flow_slug_maps()
        tasks = state.db.list_tasks_with_latest_run()
        task_groups: Dict[str, Dict[str, Any]] = {}
        for task in tasks:
            panel_id = str(task["panel_id"])
            group = task_groups.setdefault(
                panel_id,
                {
                    "panel_id": panel_id,
                    "title": panel_titles.get(panel_id, panel_id),
                    "slug": flow_slug_map.get(panel_id, panel_id),
                    "tasks": [],
                },
            )
            group["tasks"].append(
                {
                    "task_id": task["task_id"],
                    "slug": task_slug_map.get(str(task["task_id"]), str(task["task_id"])),
                    "title": task["title"],
                    "task_type": task["task_type"],
                    "icon_idle": task["icon_idle"],
                    "icon_running": task["icon_running"],
                    "run": {
                        "run_id": task.get("run_id"),
                        "status": task.get("run_status") or "idle",
                        "stop_mode": task.get("stop_mode"),
                        "started_at": task.get("started_at"),
                        "finished_at": task.get("finished_at"),
                        "heartbeat_at": task.get("heartbeat_at"),
                        "exit_code": task.get("exit_code"),
                        "error_text": task.get("error_text"),
                        "summary": (
                            task.get("run_summary")
                            if isinstance(task.get("run_summary"), dict) and task.get("run_summary")
                            else None
                        ),
                    },
                }
            )

        active_runs = state.db.list_active_runs()

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "global": self._build_global_payload(active_runs=active_runs),
            "flows": sorted(task_groups.values(), key=lambda item: str(item.get("title", "")).lower()),
        }

    def build_task_detail_payload(self, task_key: str, limit: int = 20) -> Dict[str, Any]:
        """Compose one task detail payload with run history."""
        state = self._state()
        task = self._resolve_task_identifier(task_key)
        task_slug_map, _ = self._task_slug_maps()
        task_id = str(task["task_id"])

        panel = state.db.get_panel(str(task["panel_id"])) or {
            "panel_id": task["panel_id"],
            "title": str(task["panel_id"]),
        }
        flow_slug_map, _ = self._flow_slug_maps()
        panel["slug"] = flow_slug_map.get(str(panel.get("panel_id") or ""), str(panel.get("panel_id") or ""))
        runs = [self._run_with_summary(run) for run in state.db.list_recent_runs_for_task(task_id, limit=limit)]
        status_counts: Dict[str, int] = {}
        for run in runs:
            key = str(run.get("status") or "unknown")
            status_counts[key] = int(status_counts.get(key, 0)) + 1

        active_runs = state.db.list_active_runs()

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "global": self._build_global_payload(active_runs=active_runs),
            "task": {
                "task_id": task["task_id"],
                "slug": task_slug_map.get(task_id, task_id),
                "panel_id": task["panel_id"],
                "title": task["title"],
                "task_type": task["task_type"],
                "icon_idle": task["icon_idle"],
                "icon_running": task["icon_running"],
                "cwd": task["cwd"],
            },
            "panel": panel,
            "stats": {
                "total_runs": len(runs),
                "status_counts": status_counts,
                "last_run_at": runs[0].get("started_at") if runs else None,
                "last_success_at": next(
                    (run.get("finished_at") for run in runs if run.get("status") == "completed"),
                    None,
                ),
            },
            "runs": runs,
        }

    def build_flow_detail_payload(self, flow_key: str, limit_per_task: int = 20) -> Dict[str, Any]:
        """Compose one flow payload with panel stats and per-task run history."""
        state = self._state()
        panel = self._resolve_flow_identifier(flow_key)
        panel_id = str(panel["panel_id"])
        panel_titles = state.db.get_panel_title_map()
        task_slug_map, _ = self._task_slug_maps()
        flow_slug_map, _ = self._flow_slug_maps()

        tasks_with_latest = state.db.list_tasks_with_latest_run()
        tasks_by_panel: Dict[str, list[Dict[str, Any]]] = {}
        for task in tasks_with_latest:
            current_panel_id = str(task["panel_id"])
            tasks_by_panel.setdefault(current_panel_id, []).append(
                {
                    "task_id": task["task_id"],
                    "slug": task_slug_map.get(str(task["task_id"]), str(task["task_id"])),
                    "title": task["title"],
                    "task_type": task["task_type"],
                    "icon_idle": task["icon_idle"],
                    "icon_running": task["icon_running"],
                    "run": {
                        "run_id": task.get("run_id"),
                        "status": task.get("run_status") or "idle",
                        "stop_mode": task.get("stop_mode"),
                        "started_at": task.get("started_at"),
                        "finished_at": task.get("finished_at"),
                        "heartbeat_at": task.get("heartbeat_at"),
                        "exit_code": task.get("exit_code"),
                        "error_text": task.get("error_text"),
                        "summary": (
                            task.get("run_summary")
                            if isinstance(task.get("run_summary"), dict) and task.get("run_summary")
                            else None
                        ),
                    },
                }
            )

        workflows = state.db.list_workflows_with_latest_run(panel_id=panel_id)
        panel_payloads = self._build_panel_payloads(
            tasks_by_panel=tasks_by_panel,
            panel_titles=panel_titles,
            workflows=state.db.list_workflows_with_latest_run(),
        )
        flow_payload = dict(
            panel_payloads.get(panel_id)
            or {
                "panel_id": panel_id,
                "title": panel_titles.get(panel_id, panel_id),
                "description": "",
                "status_counts": {},
                "stats_cards": [],
                "tasks": [],
            }
        )
        flow_payload["slug"] = flow_slug_map.get(panel_id, panel_id)

        ops = self._ops()
        task_items: list[Dict[str, Any]] = []
        for task in tasks_by_panel.get(panel_id, []):
            task_id = str(task["task_id"])
            runs = [
                self._run_with_summary(run)
                for run in state.db.list_recent_runs_for_task(task_id, limit=limit_per_task)
            ]
            latest_run = runs[0] if runs else {
                "run_id": None,
                "status": "idle",
                "stop_mode": None,
                "started_at": None,
                "finished_at": None,
                "heartbeat_at": None,
                "pid": None,
                "exit_code": None,
                "error_text": None,
                "summary": ops["build_default_run_summary"]({"status": "idle"}),
            }
            task_items.append(
                {
                    "task_id": task_id,
                    "slug": task["slug"],
                    "title": task["title"],
                    "task_type": task["task_type"],
                    "icon_idle": task["icon_idle"],
                    "icon_running": task["icon_running"],
                    "run": latest_run,
                    "runs": runs,
                }
            )

        active_runs = state.db.list_active_runs()

        workflow_items = [
            {
                "workflow_id": row["workflow_id"],
                "title": row["title"],
                "description": row.get("description") or "",
                "run": {
                    "workflow_run_id": row.get("workflow_run_id"),
                    "status": row.get("run_status") or "idle",
                    "started_at": row.get("started_at"),
                    "finished_at": row.get("finished_at"),
                    "error_text": row.get("error_text"),
                },
            }
            for row in workflows
        ]

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "global": self._build_global_payload(active_runs=active_runs),
            "flow": flow_payload,
            "tasks": sorted(task_items, key=lambda item: str(item.get("title", "")).lower()),
            "workflows": workflow_items,
        }

    def build_library_payload(self) -> Dict[str, Any]:
        """Compose library page payload with external dataset stats."""
        state = self._state()
        ops = self._ops()
        active_runs = state.db.list_active_runs()

        last_eval_run = state.db.get_latest_run_for_task(ops["monocorpus_meta_evaluate_task_id"])
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "global": self._build_global_payload(active_runs=active_runs),
            "dataset": ops["get_library_dataset_stats"](),
            "last_eval_run": last_eval_run,
        }

    def build_database_state_payload(self) -> Dict[str, Any]:
        """Compose database diagnostics payload with global state."""
        state = self._state()
        ops = self._ops()
        active_runs = state.db.list_active_runs()

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "global": self._build_global_payload(active_runs=active_runs),
            "database_state": ops["build_database_state_snapshot"](state.db),
        }

    def build_classification_detail_payload(
        self,
        classification_id: int,
        *,
        docs_page: int = 1,
        docs_page_size: int = 40,
    ) -> Dict[str, Any]:
        """Compose classification detail payload with local run context."""
        state = self._state()
        ops = self._ops()
        detail = ops["get_classification_detail"](
            classification_id,
            docs_page=docs_page,
            docs_page_size=docs_page_size,
        )

        active_runs = state.db.list_active_runs()

        task_slug_map, _ = self._task_slug_maps()
        recent_eval_runs = state.db.list_recent_runs_for_task(ops["monocorpus_meta_evaluate_task_id"], limit=10)
        for run in recent_eval_runs:
            task_id = str(run.get("task_id") or "")
            run["task_slug"] = task_slug_map.get(task_id, task_id)

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "global": self._build_global_payload(active_runs=active_runs),
            "detail": detail,
            "recent_meta_evaluate_runs": recent_eval_runs,
        }

    def build_personality_payload(self) -> Dict[str, Any]:
        """Compose personality page payload with global state and overview metrics."""
        state = self._state()
        ops = self._ops()
        active_runs = state.db.list_active_runs()

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "global": self._build_global_payload(active_runs=active_runs),
            "overview": ops["get_personality_overview"](),
        }

    def build_publisher_payload(self) -> Dict[str, Any]:
        """Compose publisher page payload with global state and overview metrics."""
        state = self._state()
        ops = self._ops()
        active_runs = state.db.list_active_runs()

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "global": self._build_global_payload(active_runs=active_runs),
            "overview": ops["get_publisher_overview"](),
        }

    def build_normalization_payload(self, entity_type: str) -> Dict[str, Any]:
        """Compose normalization workbench payload with global state."""
        if entity_type not in self._normalization_entity_types:
            raise HTTPException(status_code=404, detail="Normalization entity type not found")

        state = self._state()
        ops = self._ops()
        active_runs = state.db.list_active_runs()

        label = "Personalities" if entity_type == "personality" else "Publishers"
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "entity_type": entity_type,
            "entity_label": label,
            "global": self._build_global_payload(active_runs=active_runs),
            "dashboard": ops["get_normalization_dashboard"](state.db, entity_type),
            "quality": ops["get_normalization_quality"](state.db, entity_type),
            "suggestions": ops["list_suggestions"](state.db, entity_type, limit=80),
            "history_preview": ops["list_normalization_history"](state.db, entity_type, limit=20),
        }

    def build_collections_payload(self) -> Dict[str, Any]:
        """Compose collections page payload with overview metrics."""
        state = self._state()
        ops = self._ops()
        active_runs = state.db.list_active_runs()

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "global": self._build_global_payload(active_runs=active_runs),
            "overview": ops["get_collection_overview"](),
        }
