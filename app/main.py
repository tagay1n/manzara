"""Manzara MVP API and dashboard UI server."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.db import Database
from app.control_routes import register_control_routes
from app.modules.library.insights import (
    get_classification_detail,
    get_classification_insights,
    get_merge_candidates,
    get_normalization_preview,
    list_classifications,
)
from app.modules.library.collections import (
    get_collection_insights,
    get_collection_overview,
    list_collection_items,
    list_collections as list_library_collections,
    update_collection,
)
from app.modules.library.personalities import (
    get_personality_insights,
    get_personality_overview,
    list_personalities,
)
from app.modules.library.publishers import (
    get_publisher_insights,
    get_publisher_overview,
    list_publishers,
)
from app.modules.library.normalization import (
    ENTITY_TYPES as NORMALIZATION_ENTITY_TYPES,
    bulk_link_aliases,
    bulk_reject_aliases,
    create_and_link_alias,
    create_canonical,
    get_evidence as get_normalization_evidence,
    get_merge_candidates as get_normalization_merge_candidates,
    get_normalization_dashboard,
    get_quality as get_normalization_quality,
    get_review_queue,
    link_alias,
    list_canonicals,
    list_history as list_normalization_history,
    list_suggestions,
    merge_canonicals,
    refresh_suggestions,
    reject_alias,
    undo_event,
)
from app.modules.library.stats import get_library_dataset_stats
from app.modules.maintenance.panel import (
    build_database_state_snapshot,
    build_library_panel,
    build_maintenance_panel,
)
from app.modules.maintenance.tasks import (
    MONOCORPUS_META_EVALUATE_TASK_ID,
    maintenance_task_definitions,
)
from app.modules.maintenance.workflow import (
    maintenance_backup_full_workflow_bundle,
    maintenance_backup_incr_workflow_bundle,
    library_personality_normalization_workflow_bundle,
    library_publisher_normalization_workflow_bundle,
    library_workflow_bundle,
)
from app.modules.shayan.panel import build_shayan_panel
from app.modules.shayan.tasks import shayan_task_definitions
from app.modules.shayan.workflow import (
    shayan_workflow_bundle,
)
from app.modules.oscar.panel import build_oscar_panel
from app.modules.oscar.tasks import oscar_task_definitions
from app.modules.oscar.workflow import oscar_pipeline_workflow_bundle
from app.page_routes import register_page_routes
from app.settings import Settings, load_settings
from app.run_summary import build_default_run_summary
from app.tasks import TaskRunner
from app.workflows import WorkflowService

APP_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_ROOT / "static"
_SSE_POLL_INTERVAL_SECONDS = 1.0
_SSE_HEARTBEAT_EVERY_EMPTY_POLLS = 15
_TITLE_MAX_LENGTH = 80
_SLUG_SEPARATOR_PATTERN = re.compile(r"[\s_]+")
_SLUG_CLEAN_PATTERN = re.compile(r"[^\w-]+", flags=re.UNICODE)
_PANEL_DEFS = [
    {"panel_id": "shayan", "title": "Shayan"},
    {"panel_id": "maintenance", "title": "Maintenance"},
    {"panel_id": "oscar", "title": "Oscar"},
    {"panel_id": "library", "title": "Library"},
]


class AppState:
    """Typed state holder for shared app services."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.database_url, schema=settings.database_schema)
        self.runner = TaskRunner(self.db)
        self.shutting_down = False
        self.workflow_service = WorkflowService(
            self.db,
            self.runner,
            shayan_snapshot_file=settings.shayan.latest_snapshot_file,
        )


settings = load_settings()
state = AppState(settings)
def _startup() -> None:
    """Initialize schema and seed known task/workflow definitions."""
    state.shutting_down = False
    state.db.init_schema()
    state.db.seed_panels(_PANEL_DEFS)
    task_defs = [
        *shayan_task_definitions(state.settings.shayan),
        *maintenance_task_definitions(state.settings.maintenance),
        *oscar_task_definitions(state.settings.oscar),
    ]
    state.db.seed_tasks(task_defs)
    state.db.seed_workflow_bundle(shayan_workflow_bundle(state.settings.shayan))
    state.db.seed_workflow_bundle(maintenance_backup_full_workflow_bundle())
    state.db.seed_workflow_bundle(maintenance_backup_incr_workflow_bundle())
    state.db.seed_workflow_bundle(library_workflow_bundle())
    state.db.seed_workflow_bundle(library_personality_normalization_workflow_bundle())
    state.db.seed_workflow_bundle(library_publisher_normalization_workflow_bundle())
    state.db.seed_workflow_bundle(oscar_pipeline_workflow_bundle())

    recovered_runs = state.db.recover_active_runs()
    if recovered_runs > 0:
        state.db.insert_event(
            "system.recovery",
            task_id=None,
            run_id=None,
            panel_id=None,
            payload={"recovered_runs": recovered_runs},
        )

    recovered_workflows = state.db.recover_active_workflow_runs()
    if recovered_workflows > 0:
        state.db.insert_event(
            "system.workflow_recovery",
            task_id=None,
            run_id=None,
            panel_id=None,
            payload={"recovered_workflow_runs": recovered_workflows},
        )

    if state.settings.scheduler_enabled:
        state.workflow_service.start()


def _shutdown() -> None:
    """Stop background scheduler worker on app shutdown."""
    state.shutting_down = True
    state.workflow_service.stop()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """FastAPI lifespan hook for startup/shutdown orchestration."""
    _startup()
    try:
        yield
    finally:
        _shutdown()


app = FastAPI(title="Manzara", version="0.1.0", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
register_page_routes(
    app,
    static_dir=STATIC_DIR,
    normalization_entity_types=NORMALIZATION_ENTITY_TYPES,
)
register_control_routes(
    app,
    state_provider=lambda: state,
    title_max_length=_TITLE_MAX_LENGTH,
)


@app.get("/api/health")
def health() -> Dict[str, str]:
    """Simple health probe endpoint."""
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

def _slugify(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = _SLUG_SEPARATOR_PATTERN.sub("-", text)
    text = _SLUG_CLEAN_PATTERN.sub("-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")


def _task_slug_maps() -> tuple[Dict[str, str], Dict[str, str]]:
    """Return deterministic task_id<->slug maps."""
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
        base = _slugify(task.get("title")) or _slugify(task_id) or "task"
        panel_slug = _slugify(task.get("panel_id")) or "flow"
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


def _resolve_task_identifier(task_key: str) -> Dict[str, Any]:
    """Resolve task by id or human slug."""
    task = state.db.get_task(task_key)
    if task:
        return task
    _, slug_to_task = _task_slug_maps()
    task_id = slug_to_task.get(task_key)
    if not task_id:
        raise HTTPException(status_code=404, detail="Task not found")
    resolved = state.db.get_task(task_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="Task not found")
    return resolved


def _flow_slug_maps() -> tuple[Dict[str, str], Dict[str, str]]:
    """Return deterministic panel_id<->slug maps."""
    title_map = state.db.get_panel_title_map()
    panel_ids = {str(item["panel_id"]) for item in _PANEL_DEFS}
    panel_ids.update(title_map.keys())

    used: set[str] = set()
    panel_to_slug: Dict[str, str] = {}
    slug_to_panel: Dict[str, str] = {}
    for panel_id in sorted(panel_ids):
        display_title = str(title_map.get(panel_id, panel_id))
        base = _slugify(display_title) or _slugify(panel_id) or "flow"
        candidate = base
        attempt = 1
        while candidate in used:
            if attempt == 1:
                candidate = f"{base}-{_slugify(panel_id) or panel_id}"
            else:
                candidate = f"{base}-{attempt}"
            attempt += 1
        used.add(candidate)
        panel_to_slug[panel_id] = candidate
        slug_to_panel[candidate] = panel_id
    return panel_to_slug, slug_to_panel


def _resolve_flow_identifier(flow_key: str) -> Dict[str, Any]:
    """Resolve flow by panel id or human slug."""
    panel = state.db.get_panel(flow_key)
    if panel:
        return panel
    _, slug_to_panel = _flow_slug_maps()
    panel_id = slug_to_panel.get(flow_key)
    if not panel_id:
        raise HTTPException(status_code=404, detail="Flow not found")
    resolved = state.db.get_panel(panel_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="Flow not found")
    return resolved


def _run_with_summary(run: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(run)
    summary = payload.get("summary")
    if not isinstance(summary, dict) or not summary:
        payload["summary"] = build_default_run_summary(payload)
    return payload


def _build_panel_payloads(
    *,
    tasks_by_panel: Dict[str, list[Dict[str, Any]]],
    panel_titles: Dict[str, str],
    workflows: list[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    shayan_workflows = [item for item in workflows if item.get("panel_id") == "shayan"]
    shayan_panel = build_shayan_panel(
        db=state.db,
        shayan=state.settings.shayan,
        tasks=tasks_by_panel.get("shayan", []),
        workflows=shayan_workflows,
        title=panel_titles.get("shayan", "Shayan"),
    )
    maintenance_panel = build_maintenance_panel(
        db=state.db,
        maintenance=state.settings.maintenance,
        tasks=tasks_by_panel.get("maintenance", []),
        title=panel_titles.get("maintenance", "Maintenance"),
    )
    library_panel = build_library_panel(
        db=state.db,
        maintenance=state.settings.maintenance,
        tasks=tasks_by_panel.get("library", []),
        title=panel_titles.get("library", "Library"),
    )
    oscar_panel = build_oscar_panel(
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


def build_dashboard_payload() -> Dict[str, Any]:
    """Compose dashboard payload from DB and Shayan artifacts."""
    panel_titles = state.db.get_panel_title_map()
    task_slug_map, _ = _task_slug_maps()
    flow_slug_map, _ = _flow_slug_maps()

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
    panel_payloads = _build_panel_payloads(
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
    stop_all_state = "disabled"
    if active_runs:
        stop_all_state = (
            "normal"
            if any(run.get("stop_mode") is None for run in active_runs)
            else "armed"
        )

    active_workflow_runs = len(
        [
            row
            for row in workflows
            if row.get("run_status") in {"starting", "running"}
        ]
    )

    recent_runs = state.db.list_recent_runs(20)
    for run in recent_runs:
        task_id = str(run.get("task_id") or "")
        run["task_slug"] = task_slug_map.get(task_id, task_id)
        run["summary"] = _run_with_summary(run).get("summary", {})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": {
            "active_tasks": len(active_runs),
            "active_workflows": active_workflow_runs,
            "failed_runs": len([r for r in state.db.list_recent_runs(50) if r["status"] == "failed"]),
            "stop_all_state": stop_all_state,
        },
        "panels": ordered_panels,
        "recent_runs": recent_runs,
        "scheduler": {
            "enabled": state.settings.scheduler_enabled,
        },
    }


def build_schedules_payload() -> Dict[str, Any]:
    """Compose schedules page payload from workflow/schedule state."""
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
    stop_all_state = "disabled"
    if active_runs:
        stop_all_state = (
            "normal"
            if any(run.get("stop_mode") is None for run in active_runs)
            else "armed"
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": {
            "active_tasks": len(active_runs),
            "active_workflows": len(
                [w for w in workflow_items if w["run"]["status"] in {"starting", "running"}]
            ),
            "stop_all_state": stop_all_state,
        },
        "scheduler": {
            "enabled": state.settings.scheduler_enabled,
        },
        "workflows": workflow_items,
    }


def build_tasks_payload() -> Dict[str, Any]:
    """Compose tasks page payload grouped by flow."""
    panel_titles = state.db.get_panel_title_map()
    task_slug_map, _ = _task_slug_maps()
    flow_slug_map, _ = _flow_slug_maps()
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
    stop_all_state = "disabled"
    if active_runs:
        stop_all_state = (
            "normal"
            if any(run.get("stop_mode") is None for run in active_runs)
            else "armed"
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": {
            "active_tasks": len(active_runs),
            "active_workflows": len(
                [
                    row
                    for row in state.db.list_workflows_with_latest_run()
                    if row.get("run_status") in {"starting", "running"}
                ]
            ),
            "stop_all_state": stop_all_state,
        },
        "flows": sorted(task_groups.values(), key=lambda item: str(item.get("title", "")).lower()),
    }


def build_task_detail_payload(task_key: str, limit: int = 20) -> Dict[str, Any]:
    """Compose one task detail payload with run history."""
    task = _resolve_task_identifier(task_key)
    task_slug_map, _ = _task_slug_maps()
    task_id = str(task["task_id"])

    panel = state.db.get_panel(str(task["panel_id"])) or {
        "panel_id": task["panel_id"],
        "title": str(task["panel_id"]),
    }
    flow_slug_map, _ = _flow_slug_maps()
    panel["slug"] = flow_slug_map.get(str(panel.get("panel_id") or ""), str(panel.get("panel_id") or ""))
    runs = [_run_with_summary(run) for run in state.db.list_recent_runs_for_task(task_id, limit=limit)]
    status_counts: Dict[str, int] = {}
    for run in runs:
        key = str(run.get("status") or "unknown")
        status_counts[key] = int(status_counts.get(key, 0)) + 1

    active_runs = state.db.list_active_runs()
    stop_all_state = "disabled"
    if active_runs:
        stop_all_state = (
            "normal"
            if any(run.get("stop_mode") is None for run in active_runs)
            else "armed"
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": {
            "active_tasks": len(active_runs),
            "active_workflows": len(
                [
                    row
                    for row in state.db.list_workflows_with_latest_run()
                    if row.get("run_status") in {"starting", "running"}
                ]
            ),
            "stop_all_state": stop_all_state,
        },
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


def build_flow_detail_payload(flow_key: str, limit_per_task: int = 20) -> Dict[str, Any]:
    """Compose one flow payload with panel stats and per-task run history."""
    panel = _resolve_flow_identifier(flow_key)
    panel_id = str(panel["panel_id"])
    panel_titles = state.db.get_panel_title_map()
    task_slug_map, _ = _task_slug_maps()
    flow_slug_map, _ = _flow_slug_maps()

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
    panel_payloads = _build_panel_payloads(
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

    task_items: list[Dict[str, Any]] = []
    for task in tasks_by_panel.get(panel_id, []):
        task_id = str(task["task_id"])
        runs = [_run_with_summary(run) for run in state.db.list_recent_runs_for_task(task_id, limit=limit_per_task)]
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
            "summary": build_default_run_summary({"status": "idle"}),
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
    stop_all_state = "disabled"
    if active_runs:
        stop_all_state = (
            "normal"
            if any(run.get("stop_mode") is None for run in active_runs)
            else "armed"
        )

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
        "global": {
            "active_tasks": len(active_runs),
            "active_workflows": len(
                [
                    row
                    for row in state.db.list_workflows_with_latest_run()
                    if row.get("run_status") in {"starting", "running"}
                ]
            ),
            "stop_all_state": stop_all_state,
        },
        "flow": flow_payload,
        "tasks": sorted(task_items, key=lambda item: str(item.get("title", "")).lower()),
        "workflows": workflow_items,
    }


def build_library_payload() -> Dict[str, Any]:
    """Compose library page payload with external dataset stats."""
    active_runs = state.db.list_active_runs()
    stop_all_state = "disabled"
    if active_runs:
        stop_all_state = (
            "normal"
            if any(run.get("stop_mode") is None for run in active_runs)
            else "armed"
        )

    last_eval_run = state.db.get_latest_run_for_task(MONOCORPUS_META_EVALUATE_TASK_ID)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": {
            "active_tasks": len(active_runs),
            "active_workflows": len(
                [
                    row
                    for row in state.db.list_workflows_with_latest_run()
                    if row.get("run_status") in {"starting", "running"}
                ]
            ),
            "stop_all_state": stop_all_state,
        },
        "dataset": get_library_dataset_stats(),
        "last_eval_run": last_eval_run,
    }


def build_database_state_payload() -> Dict[str, Any]:
    """Compose database diagnostics payload with global state."""
    active_runs = state.db.list_active_runs()
    stop_all_state = "disabled"
    if active_runs:
        stop_all_state = (
            "normal"
            if any(run.get("stop_mode") is None for run in active_runs)
            else "armed"
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": {
            "active_tasks": len(active_runs),
            "active_workflows": len(
                [
                    row
                    for row in state.db.list_workflows_with_latest_run()
                    if row.get("run_status") in {"starting", "running"}
                ]
            ),
            "stop_all_state": stop_all_state,
        },
        "database_state": build_database_state_snapshot(state.db),
    }


def build_classification_detail_payload(
    classification_id: int,
    *,
    docs_page: int = 1,
    docs_page_size: int = 40,
) -> Dict[str, Any]:
    """Compose classification detail payload with local run context."""
    detail = get_classification_detail(
        classification_id,
        docs_page=docs_page,
        docs_page_size=docs_page_size,
    )

    active_runs = state.db.list_active_runs()
    stop_all_state = "disabled"
    if active_runs:
        stop_all_state = (
            "normal"
            if any(run.get("stop_mode") is None for run in active_runs)
            else "armed"
        )

    task_slug_map, _ = _task_slug_maps()
    recent_eval_runs = state.db.list_recent_runs_for_task(MONOCORPUS_META_EVALUATE_TASK_ID, limit=10)
    for run in recent_eval_runs:
        task_id = str(run.get("task_id") or "")
        run["task_slug"] = task_slug_map.get(task_id, task_id)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": {
            "active_tasks": len(active_runs),
            "active_workflows": len(
                [
                    row
                    for row in state.db.list_workflows_with_latest_run()
                    if row.get("run_status") in {"starting", "running"}
                ]
            ),
            "stop_all_state": stop_all_state,
        },
        "detail": detail,
        "recent_meta_evaluate_runs": recent_eval_runs,
    }


def build_personality_payload() -> Dict[str, Any]:
    """Compose personality page payload with global state and overview metrics."""
    active_runs = state.db.list_active_runs()
    stop_all_state = "disabled"
    if active_runs:
        stop_all_state = (
            "normal"
            if any(run.get("stop_mode") is None for run in active_runs)
            else "armed"
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": {
            "active_tasks": len(active_runs),
            "active_workflows": len(
                [
                    row
                    for row in state.db.list_workflows_with_latest_run()
                    if row.get("run_status") in {"starting", "running"}
                ]
            ),
            "stop_all_state": stop_all_state,
        },
        "overview": get_personality_overview(),
    }


def build_publisher_payload() -> Dict[str, Any]:
    """Compose publisher page payload with global state and overview metrics."""
    active_runs = state.db.list_active_runs()
    stop_all_state = "disabled"
    if active_runs:
        stop_all_state = (
            "normal"
            if any(run.get("stop_mode") is None for run in active_runs)
            else "armed"
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": {
            "active_tasks": len(active_runs),
            "active_workflows": len(
                [
                    row
                    for row in state.db.list_workflows_with_latest_run()
                    if row.get("run_status") in {"starting", "running"}
                ]
            ),
            "stop_all_state": stop_all_state,
        },
        "overview": get_publisher_overview(),
    }


def build_normalization_payload(entity_type: str) -> Dict[str, Any]:
    """Compose normalization workbench payload with global state."""
    if entity_type not in NORMALIZATION_ENTITY_TYPES:
        raise HTTPException(status_code=404, detail="Normalization entity type not found")

    active_runs = state.db.list_active_runs()
    stop_all_state = "disabled"
    if active_runs:
        stop_all_state = (
            "normal"
            if any(run.get("stop_mode") is None for run in active_runs)
            else "armed"
        )

    label = "Personalities" if entity_type == "personality" else "Publishers"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entity_type": entity_type,
        "entity_label": label,
        "global": {
            "active_tasks": len(active_runs),
            "active_workflows": len(
                [
                    row
                    for row in state.db.list_workflows_with_latest_run()
                    if row.get("run_status") in {"starting", "running"}
                ]
            ),
            "stop_all_state": stop_all_state,
        },
        "dashboard": get_normalization_dashboard(state.db, entity_type),
        "quality": get_normalization_quality(state.db, entity_type),
        "suggestions": list_suggestions(state.db, entity_type, limit=80),
        "history_preview": list_normalization_history(state.db, entity_type, limit=20),
    }


def build_collections_payload() -> Dict[str, Any]:
    """Compose collections page payload with overview metrics."""
    active_runs = state.db.list_active_runs()
    stop_all_state = "disabled"
    if active_runs:
        stop_all_state = (
            "normal"
            if any(run.get("stop_mode") is None for run in active_runs)
            else "armed"
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": {
            "active_tasks": len(active_runs),
            "active_workflows": len(
                [
                    row
                    for row in state.db.list_workflows_with_latest_run()
                    if row.get("run_status") in {"starting", "running"}
                ]
            ),
            "stop_all_state": stop_all_state,
        },
        "overview": get_collection_overview(),
    }


@app.get("/api/dashboard")
def get_dashboard() -> JSONResponse:
    """Return current dashboard state."""
    return JSONResponse(build_dashboard_payload())


@app.get("/api/schedules")
def get_schedules() -> JSONResponse:
    """Return workflows and schedule configuration state."""
    return JSONResponse(build_schedules_payload())


@app.get("/api/tasks")
def get_tasks() -> JSONResponse:
    """Return all tasks grouped by flow."""
    return JSONResponse(build_tasks_payload())


@app.get("/api/tasks/{task_id}")
def get_task_detail(
    task_id: str,
    limit: int = Query(20, ge=1, le=400),
) -> JSONResponse:
    """Return one task with run history (task id or slug)."""
    return JSONResponse(build_task_detail_payload(task_id, limit=limit))


@app.get("/api/flows/{flow_id_or_slug}")
def get_flow_detail(
    flow_id_or_slug: str,
    limit_per_task: int = Query(20, ge=1, le=200),
) -> JSONResponse:
    """Return one flow with panel stats and per-task run history."""
    return JSONResponse(build_flow_detail_payload(flow_id_or_slug, limit_per_task=limit_per_task))


@app.get("/api/library")
def get_library() -> JSONResponse:
    """Return library applicability dataset statistics."""
    return JSONResponse(build_library_payload())


@app.get("/api/database/state")
def get_database_state() -> JSONResponse:
    """Return database diagnostics snapshot."""
    return JSONResponse(build_database_state_payload())


@app.get("/api/library/personalities")
def get_library_personalities() -> JSONResponse:
    """Return personality overview payload."""
    return JSONResponse(build_personality_payload())


@app.get("/api/library/personalities/table")
def get_library_personalities_table(
    search: str = Query("", max_length=120),
    script_label: str = Query("", max_length=40),
    min_docs: int = Query(0, ge=0),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    sort: str = Query("docs_desc", max_length=40),
) -> JSONResponse:
    """Return paginated personalities table."""
    payload = list_personalities(
        search=search,
        script_label=script_label,
        min_docs=min_docs,
        page=page,
        page_size=page_size,
        sort=sort,
    )
    return JSONResponse(payload)


@app.get("/api/library/personalities/insights")
def get_library_personalities_insights(
    cluster_limit: int = Query(24, ge=1, le=100),
    queue_limit: int = Query(40, ge=1, le=200),
) -> JSONResponse:
    """Return personalities insight tabs payload."""
    payload = get_personality_insights(
        cluster_limit=cluster_limit,
        queue_limit=queue_limit,
    )
    return JSONResponse(payload)


@app.get("/api/library/publishers")
def get_library_publishers() -> JSONResponse:
    """Return publisher overview payload."""
    return JSONResponse(build_publisher_payload())


@app.get("/api/library/collections")
def get_library_collections() -> JSONResponse:
    """Return collection overview payload."""
    return JSONResponse(build_collections_payload())


@app.get("/api/library/collections/table")
def get_library_collections_table(
    search: str = Query("", max_length=120),
    status: str = Query("", max_length=40),
    include: str = Query("all", max_length=10),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    sort: str = Query("updated_desc", max_length=40),
) -> JSONResponse:
    """Return paginated collections table."""
    payload = list_library_collections(
        search=search,
        status=status,
        include=include,
        page=page,
        page_size=page_size,
        sort=sort,
    )
    return JSONResponse(payload)


@app.get("/api/library/collections/insights")
def get_library_collections_insights(
    cluster_limit: int = Query(24, ge=1, le=200),
    queue_limit: int = Query(40, ge=1, le=200),
) -> JSONResponse:
    """Return collection insight tabs payload."""
    payload = get_collection_insights(
        cluster_limit=cluster_limit,
        queue_limit=queue_limit,
    )
    return JSONResponse(payload)


@app.get("/api/library/collections/{collection_id}/items")
def get_library_collection_items(
    collection_id: int,
    limit: int = Query(400, ge=1, le=2000),
) -> JSONResponse:
    """Return one collection with linked items."""
    payload = list_collection_items(collection_id, limit=limit)
    return JSONResponse(payload)


@app.patch("/api/library/collections/{collection_id}")
def patch_library_collection(
    collection_id: int,
    payload: Dict[str, Any] = Body(...),
) -> JSONResponse:
    """Patch collection review status/title/notes/include settings."""
    try:
        result = update_collection(state.db, collection_id, updates=payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@app.get("/api/library/publishers/table")
def get_library_publishers_table(
    search: str = Query("", max_length=120),
    script_label: str = Query("", max_length=40),
    min_docs: int = Query(0, ge=0),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    sort: str = Query("docs_desc", max_length=40),
) -> JSONResponse:
    """Return paginated publishers table."""
    payload = list_publishers(
        search=search,
        script_label=script_label,
        min_docs=min_docs,
        page=page,
        page_size=page_size,
        sort=sort,
    )
    return JSONResponse(payload)


@app.get("/api/library/publishers/insights")
def get_library_publishers_insights(
    cluster_limit: int = Query(24, ge=1, le=100),
    queue_limit: int = Query(40, ge=1, le=200),
) -> JSONResponse:
    """Return publishers insight tabs payload."""
    payload = get_publisher_insights(
        cluster_limit=cluster_limit,
        queue_limit=queue_limit,
    )
    return JSONResponse(payload)


def _require_normalization_entity(entity_type: str) -> str:
    normalized = str(entity_type or "").strip().lower()
    if normalized not in NORMALIZATION_ENTITY_TYPES:
        raise HTTPException(status_code=404, detail="Normalization entity type not found")
    return normalized


@app.get("/api/library/normalization/{entity_type}")
def get_library_normalization(entity_type: str) -> JSONResponse:
    """Return normalization workbench summary payload."""
    normalized = _require_normalization_entity(entity_type)
    return JSONResponse(build_normalization_payload(normalized))


@app.get("/api/library/normalization/{entity_type}/queue")
def get_library_normalization_queue(
    entity_type: str,
    status: str = Query("all", max_length=40),
    search: str = Query("", max_length=120),
    script_label: str = Query("", max_length=40),
    min_docs: int = Query(0, ge=0),
    page: int = Query(1, ge=1),
    page_size: int = Query(40, ge=1, le=200),
) -> JSONResponse:
    """Return normalization review queue."""
    normalized = _require_normalization_entity(entity_type)
    payload = get_review_queue(
        state.db,
        normalized,
        status=status,
        search=search,
        script_label=script_label,
        min_docs=min_docs,
        page=page,
        page_size=page_size,
    )
    return JSONResponse(payload)


@app.get("/api/library/normalization/{entity_type}/canonicals")
def get_library_normalization_canonicals(
    entity_type: str,
    search: str = Query("", max_length=160),
) -> JSONResponse:
    """Return canonical registry entries for normalization entity."""
    normalized = _require_normalization_entity(entity_type)
    return JSONResponse(list_canonicals(state.db, normalized, search=search))


@app.post("/api/library/normalization/{entity_type}/canonicals")
def create_library_normalization_canonical(
    entity_type: str,
    payload: Dict[str, Any] = Body(...),
) -> JSONResponse:
    """Create one canonical entry."""
    normalized = _require_normalization_entity(entity_type)
    display_name = str(payload.get("display_name") or "").strip()
    notes = str(payload.get("notes") or "").strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="display_name is required")
    try:
        result = create_canonical(
            state.db,
            normalized,
            display_name=display_name,
            notes=notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@app.post("/api/library/normalization/{entity_type}/decisions/link")
def link_library_normalization_alias(
    entity_type: str,
    payload: Dict[str, Any] = Body(...),
) -> JSONResponse:
    """Link alias to canonical."""
    normalized = _require_normalization_entity(entity_type)
    raw_name = str(payload.get("raw_name") or "").strip()
    if not raw_name:
        raise HTTPException(status_code=400, detail="raw_name is required")
    canonical_id = payload.get("canonical_id")
    try:
        canonical_int = int(canonical_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="canonical_id must be an integer")

    confidence_raw = payload.get("confidence")
    confidence = None
    if confidence_raw is not None and str(confidence_raw).strip() != "":
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="confidence must be a number")

    suggestion_ids_raw = payload.get("suggestion_ids") or []
    suggestion_ids = [int(item) for item in suggestion_ids_raw if str(item).strip()]

    try:
        result = link_alias(
            state.db,
            normalized,
            raw_name=raw_name,
            canonical_id=canonical_int,
            source=str(payload.get("source") or "manual"),
            confidence=confidence if confidence is not None else 1.0,
            reason=str(payload.get("reason") or ""),
            suggestion_ids=suggestion_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@app.post("/api/library/normalization/{entity_type}/decisions/create-link")
def create_and_link_library_normalization_alias(
    entity_type: str,
    payload: Dict[str, Any] = Body(...),
) -> JSONResponse:
    """Create canonical and link alias in one action."""
    normalized = _require_normalization_entity(entity_type)
    raw_name = str(payload.get("raw_name") or "").strip()
    display_name = str(payload.get("display_name") or "").strip()
    suggestion_ids_raw = payload.get("suggestion_ids") or []
    suggestion_ids = [int(item) for item in suggestion_ids_raw if str(item).strip()]
    if not raw_name:
        raise HTTPException(status_code=400, detail="raw_name is required")
    if not display_name:
        raise HTTPException(status_code=400, detail="display_name is required")

    try:
        result = create_and_link_alias(
            state.db,
            normalized,
            raw_name=raw_name,
            display_name=display_name,
            reason=str(payload.get("reason") or ""),
            suggestion_ids=suggestion_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@app.post("/api/library/normalization/{entity_type}/decisions/reject")
def reject_library_normalization_alias(
    entity_type: str,
    payload: Dict[str, Any] = Body(...),
) -> JSONResponse:
    """Reject alias from normalization queue."""
    normalized = _require_normalization_entity(entity_type)
    raw_name = str(payload.get("raw_name") or "").strip()
    if not raw_name:
        raise HTTPException(status_code=400, detail="raw_name is required")
    suggestion_ids_raw = payload.get("suggestion_ids") or []
    suggestion_ids = [int(item) for item in suggestion_ids_raw if str(item).strip()]

    try:
        result = reject_alias(
            state.db,
            normalized,
            raw_name=raw_name,
            reason=str(payload.get("reason") or ""),
            suggestion_ids=suggestion_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@app.post("/api/library/normalization/{entity_type}/bulk/link")
def bulk_link_library_normalization_aliases(
    entity_type: str,
    payload: Dict[str, Any] = Body(...),
) -> JSONResponse:
    """Bulk-link aliases to a canonical."""
    normalized = _require_normalization_entity(entity_type)
    raw_names = payload.get("raw_names") or []
    canonical_id = payload.get("canonical_id")
    try:
        canonical_int = int(canonical_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="canonical_id must be an integer")

    try:
        result = bulk_link_aliases(
            state.db,
            normalized,
            raw_names=[str(item) for item in raw_names],
            canonical_id=canonical_int,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@app.post("/api/library/normalization/{entity_type}/bulk/reject")
def bulk_reject_library_normalization_aliases(
    entity_type: str,
    payload: Dict[str, Any] = Body(...),
) -> JSONResponse:
    """Bulk-reject aliases from queue."""
    normalized = _require_normalization_entity(entity_type)
    raw_names = payload.get("raw_names") or []
    try:
        result = bulk_reject_aliases(
            state.db,
            normalized,
            raw_names=[str(item) for item in raw_names],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@app.get("/api/library/normalization/{entity_type}/suggestions")
def get_library_normalization_suggestions(
    entity_type: str,
    limit: int = Query(200, ge=1, le=1000),
) -> JSONResponse:
    """Return open suggestions for normalization queue."""
    normalized = _require_normalization_entity(entity_type)
    return JSONResponse(list_suggestions(state.db, normalized, limit=limit))


@app.post("/api/library/normalization/{entity_type}/suggestions/refresh")
def refresh_library_normalization_suggestions(
    entity_type: str,
    payload: Dict[str, Any] = Body(default={}),
) -> JSONResponse:
    """Regenerate normalization suggestions."""
    normalized = _require_normalization_entity(entity_type)
    limit = payload.get("limit", 120)
    use_gemini = payload.get("use_gemini", True)
    try:
        result = refresh_suggestions(
            state.db,
            normalized,
            limit=int(limit),
            use_gemini=bool(use_gemini),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@app.get("/api/library/normalization/{entity_type}/merge-candidates")
def get_library_normalization_merge_candidates(
    entity_type: str,
    min_score: float = Query(0.84, ge=0.0, le=1.0),
    limit: int = Query(80, ge=1, le=300),
) -> JSONResponse:
    """Return possible canonical merge candidates."""
    normalized = _require_normalization_entity(entity_type)
    return JSONResponse(
        get_normalization_merge_candidates(
            state.db,
            normalized,
            min_score=min_score,
            limit=limit,
        )
    )


@app.post("/api/library/normalization/{entity_type}/merge")
def merge_library_normalization_canonicals(
    entity_type: str,
    payload: Dict[str, Any] = Body(...),
) -> JSONResponse:
    """Merge source canonical into target canonical."""
    normalized = _require_normalization_entity(entity_type)
    try:
        source_canonical_id = int(payload.get("source_canonical_id"))
        target_canonical_id = int(payload.get("target_canonical_id"))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="source_canonical_id and target_canonical_id must be integers",
        )

    try:
        result = merge_canonicals(
            state.db,
            normalized,
            source_canonical_id=source_canonical_id,
            target_canonical_id=target_canonical_id,
            reason=str(payload.get("reason") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@app.get("/api/library/normalization/{entity_type}/history")
def get_library_normalization_history(
    entity_type: str,
    limit: int = Query(200, ge=1, le=1000),
) -> JSONResponse:
    """Return normalization action history."""
    normalized = _require_normalization_entity(entity_type)
    return JSONResponse(list_normalization_history(state.db, normalized, limit=limit))


@app.post("/api/library/normalization/{entity_type}/history/{event_id}/undo")
def undo_library_normalization_history_event(
    entity_type: str,
    event_id: int,
) -> JSONResponse:
    """Undo one normalization event by id."""
    normalized = _require_normalization_entity(entity_type)
    try:
        result = undo_event(state.db, normalized, event_id=event_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@app.get("/api/library/normalization/{entity_type}/quality")
def get_library_normalization_quality(entity_type: str) -> JSONResponse:
    """Return quality metrics for normalization workbench."""
    normalized = _require_normalization_entity(entity_type)
    return JSONResponse(get_normalization_quality(state.db, normalized))


@app.get("/api/library/normalization/{entity_type}/evidence")
def get_library_normalization_alias_evidence(
    entity_type: str,
    raw_name: str = Query(..., min_length=1, max_length=240),
    limit: int = Query(20, ge=1, le=200),
) -> JSONResponse:
    """Return sample docs where alias appears."""
    normalized = _require_normalization_entity(entity_type)
    try:
        payload = get_normalization_evidence(
            normalized,
            raw_name=raw_name,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(payload)


@app.get("/api/library/classifications")
def get_library_classifications(
    search: str = Query("", max_length=120),
    status: str = Query("", max_length=40),
    ddc_prefix: str = Query("", max_length=40),
    min_usage: int = Query(0, ge=0),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    sort: str = Query("usage_desc", max_length=40),
) -> JSONResponse:
    """Return paginated classification table."""
    payload = list_classifications(
        search=search,
        status=status,
        ddc_prefix=ddc_prefix,
        min_usage=min_usage,
        page=page,
        page_size=page_size,
        sort=sort,
    )
    return JSONResponse(payload)


@app.get("/api/library/classifications/insights")
def get_library_classification_insights(
    row_limit: int = Query(5000, ge=1, le=20000),
    duplicate_limit: int = Query(25, ge=1, le=200),
    unclassified_limit: int = Query(30, ge=1, le=200),
) -> JSONResponse:
    """Return hierarchy, distribution, duplicates, and unclassified queue."""
    payload = get_classification_insights(
        row_limit=row_limit,
        duplicate_limit=duplicate_limit,
        unclassified_limit=unclassified_limit,
    )
    return JSONResponse(payload)


@app.get("/api/library/classifications/normalization-preview")
def get_library_classification_normalization_preview(
    drop_segments: str = Query("Turkic literature", max_length=300),
    limit: int = Query(120, ge=1, le=500),
    row_limit: int = Query(5000, ge=1, le=20000),
) -> JSONResponse:
    """Preview simplification rules before applying any merge."""
    segments = [item.strip() for item in drop_segments.split(",") if item.strip()]
    payload = get_normalization_preview(
        drop_segments=segments,
        limit=limit,
        row_limit=row_limit,
    )
    return JSONResponse(payload)


@app.get("/api/library/classifications/merge-candidates")
def get_library_classification_merge_candidates(
    limit: int = Query(80, ge=1, le=300),
    min_score: float = Query(0.78, ge=0.0, le=1.0),
    row_limit: int = Query(1200, ge=10, le=10000),
) -> JSONResponse:
    """Return ranked near-duplicate classification merge suggestions."""
    payload = get_merge_candidates(
        limit=limit,
        min_score=min_score,
        row_limit=row_limit,
    )
    return JSONResponse(payload)


@app.get("/api/library/classifications/{classification_id}")
def get_library_classification_detail(
    classification_id: int,
    docs_page: int = Query(1, ge=1),
    docs_page_size: int = Query(40, ge=1, le=200),
) -> JSONResponse:
    """Return one classification detail."""
    return JSONResponse(
        build_classification_detail_payload(
            classification_id,
            docs_page=docs_page,
            docs_page_size=docs_page_size,
        )
    )


@app.get("/api/runs/{run_id}/logs")
def run_logs(
    run_id: int,
    after_log_id: int = Query(0, ge=0),
    before_log_id: Optional[int] = Query(None, gt=0),
    tail: bool = Query(False),
    limit: int = Query(400, ge=1, le=2000),
) -> JSONResponse:
    """Return logs for one run with cursor pagination (after/before/tail)."""
    run = state.db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    if tail and (after_log_id > 0 or before_log_id is not None):
        raise HTTPException(
            status_code=400,
            detail="tail mode cannot be combined with after_log_id or before_log_id",
        )
    if before_log_id is not None and after_log_id > 0:
        raise HTTPException(
            status_code=400,
            detail="before_log_id cannot be combined with after_log_id",
        )

    lines = state.db.get_logs(
        run_id=run_id,
        after_log_id=after_log_id,
        before_log_id=before_log_id,
        tail=tail,
        limit=limit,
    )
    next_after_log_id = int(lines[-1]["log_id"]) if lines else int(after_log_id or 0)
    if lines:
        next_before_log_id = int(lines[0]["log_id"])
    elif before_log_id is not None:
        next_before_log_id = int(before_log_id)
    else:
        next_before_log_id = 0

    has_more_before = (
        state.db.has_logs_before(run_id, next_before_log_id)
        if next_before_log_id > 0
        else False
    )

    return JSONResponse(
        {
            "run": run,
            "lines": lines,
            "next_after_log_id": next_after_log_id,
            "next_before_log_id": next_before_log_id,
            "has_more_before": has_more_before,
        }
    )


@app.get("/api/events/stream")
async def events_stream(
    request: Request,
    after_event_id: int = Query(0, ge=0),
) -> StreamingResponse:
    """Server-Sent Events stream for near-real-time dashboard updates."""

    header_last_event: Optional[str] = request.headers.get("last-event-id")
    cursor = after_event_id
    if header_last_event is not None:
        try:
            cursor = max(cursor, int(header_last_event))
        except ValueError:
            pass

    async def event_generator():
        nonlocal cursor
        heartbeat_counter = 0
        try:
            while True:
                if state.shutting_down:
                    break

                if await request.is_disconnected():
                    break

                events = state.db.get_events_after(cursor, limit=200)
                if events:
                    for event in events:
                        cursor = int(event["event_id"])
                        data = json.dumps(event, ensure_ascii=False)
                        yield f"id: {cursor}\nevent: {event['type']}\ndata: {data}\n\n"
                    heartbeat_counter = 0
                    # Keep loop cooperative for cancellation/shutdown.
                    await asyncio.sleep(0)
                else:
                    heartbeat_counter += 1
                    if heartbeat_counter >= _SSE_HEARTBEAT_EVERY_EMPTY_POLLS:
                        yield ": heartbeat\n\n"
                        heartbeat_counter = 0
                    await asyncio.sleep(_SSE_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
