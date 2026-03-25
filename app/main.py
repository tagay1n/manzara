"""Manzara MVP API and dashboard UI server."""

from __future__ import annotations

from contextlib import asynccontextmanager
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from app.db import Database
from app.control_routes import register_control_routes
from app.core_read_routes import register_core_read_routes
from app.library_classification_routes import register_library_classification_routes
from app.library_entities_routes import register_library_entities_routes
from app.library_normalization_routes import register_library_normalization_routes
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
from app.stream_routes import register_stream_routes
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
_stream_route_handlers = register_stream_routes(
    app,
    state_provider=lambda: state,
    sse_poll_interval_seconds=_SSE_POLL_INTERVAL_SECONDS,
    sse_heartbeat_every_empty_polls=_SSE_HEARTBEAT_EVERY_EMPTY_POLLS,
)
run_logs = _stream_route_handlers["run_logs"]
events_stream = _stream_route_handlers["events_stream"]


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


def _count_active_workflows(workflows: Optional[list[Dict[str, Any]]] = None) -> int:
    rows = workflows if workflows is not None else state.db.list_workflows_with_latest_run()
    return len([row for row in rows if row.get("run_status") in {"starting", "running"}])


def _resolve_stop_all_state(active_runs: list[Dict[str, Any]]) -> str:
    if not active_runs:
        return "disabled"
    if any(run.get("stop_mode") is None for run in active_runs):
        return "normal"
    return "armed"


def _build_global_payload(
    *,
    active_runs: Optional[list[Dict[str, Any]]] = None,
    active_workflows: Optional[int] = None,
    include_failed_runs: bool = False,
) -> Dict[str, Any]:
    runs = active_runs if active_runs is not None else state.db.list_active_runs()
    payload: Dict[str, Any] = {
        "active_tasks": len(runs),
        "active_workflows": (
            active_workflows
            if active_workflows is not None
            else _count_active_workflows()
        ),
        "stop_all_state": _resolve_stop_all_state(runs),
    }
    if include_failed_runs:
        payload["failed_runs"] = len(
            [run for run in state.db.list_recent_runs(50) if run["status"] == "failed"]
        )
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
    active_workflow_runs = _count_active_workflows(workflows)

    recent_runs = state.db.list_recent_runs(20)
    for run in recent_runs:
        task_id = str(run.get("task_id") or "")
        run["task_slug"] = task_slug_map.get(task_id, task_id)
        run["summary"] = _run_with_summary(run).get("summary", {})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": _build_global_payload(
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
    active_workflows = len(
        [workflow for workflow in workflow_items if workflow["run"]["status"] in {"starting", "running"}]
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": _build_global_payload(
            active_runs=active_runs,
            active_workflows=active_workflows,
        ),
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

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": _build_global_payload(active_runs=active_runs),
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

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": _build_global_payload(active_runs=active_runs),
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
        "global": _build_global_payload(active_runs=active_runs),
        "flow": flow_payload,
        "tasks": sorted(task_items, key=lambda item: str(item.get("title", "")).lower()),
        "workflows": workflow_items,
    }


def build_library_payload() -> Dict[str, Any]:
    """Compose library page payload with external dataset stats."""
    active_runs = state.db.list_active_runs()

    last_eval_run = state.db.get_latest_run_for_task(MONOCORPUS_META_EVALUATE_TASK_ID)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": _build_global_payload(active_runs=active_runs),
        "dataset": get_library_dataset_stats(),
        "last_eval_run": last_eval_run,
    }


def build_database_state_payload() -> Dict[str, Any]:
    """Compose database diagnostics payload with global state."""
    active_runs = state.db.list_active_runs()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": _build_global_payload(active_runs=active_runs),
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

    task_slug_map, _ = _task_slug_maps()
    recent_eval_runs = state.db.list_recent_runs_for_task(MONOCORPUS_META_EVALUATE_TASK_ID, limit=10)
    for run in recent_eval_runs:
        task_id = str(run.get("task_id") or "")
        run["task_slug"] = task_slug_map.get(task_id, task_id)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": _build_global_payload(active_runs=active_runs),
        "detail": detail,
        "recent_meta_evaluate_runs": recent_eval_runs,
    }


def build_personality_payload() -> Dict[str, Any]:
    """Compose personality page payload with global state and overview metrics."""
    active_runs = state.db.list_active_runs()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": _build_global_payload(active_runs=active_runs),
        "overview": get_personality_overview(),
    }


def build_publisher_payload() -> Dict[str, Any]:
    """Compose publisher page payload with global state and overview metrics."""
    active_runs = state.db.list_active_runs()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": _build_global_payload(active_runs=active_runs),
        "overview": get_publisher_overview(),
    }


def build_normalization_payload(entity_type: str) -> Dict[str, Any]:
    """Compose normalization workbench payload with global state."""
    if entity_type not in NORMALIZATION_ENTITY_TYPES:
        raise HTTPException(status_code=404, detail="Normalization entity type not found")

    active_runs = state.db.list_active_runs()

    label = "Personalities" if entity_type == "personality" else "Publishers"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entity_type": entity_type,
        "entity_label": label,
        "global": _build_global_payload(active_runs=active_runs),
        "dashboard": get_normalization_dashboard(state.db, entity_type),
        "quality": get_normalization_quality(state.db, entity_type),
        "suggestions": list_suggestions(state.db, entity_type, limit=80),
        "history_preview": list_normalization_history(state.db, entity_type, limit=20),
    }


def build_collections_payload() -> Dict[str, Any]:
    """Compose collections page payload with overview metrics."""
    active_runs = state.db.list_active_runs()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": _build_global_payload(active_runs=active_runs),
        "overview": get_collection_overview(),
    }


register_library_normalization_routes(
    app,
    state_provider=lambda: state,
    normalization_entity_types=NORMALIZATION_ENTITY_TYPES,
    build_normalization_payload=build_normalization_payload,
    operations_provider=lambda: {
        "get_review_queue": get_review_queue,
        "list_canonicals": list_canonicals,
        "create_canonical": create_canonical,
        "link_alias": link_alias,
        "create_and_link_alias": create_and_link_alias,
        "reject_alias": reject_alias,
        "bulk_link_aliases": bulk_link_aliases,
        "bulk_reject_aliases": bulk_reject_aliases,
        "list_suggestions": list_suggestions,
        "refresh_suggestions": refresh_suggestions,
        "get_normalization_merge_candidates": get_normalization_merge_candidates,
        "merge_canonicals": merge_canonicals,
        "list_normalization_history": list_normalization_history,
        "undo_event": undo_event,
        "get_normalization_quality": get_normalization_quality,
        "get_normalization_evidence": get_normalization_evidence,
    },
)
register_library_classification_routes(
    app,
    operations_provider=lambda: {
        "list_classifications": list_classifications,
        "get_classification_insights": get_classification_insights,
        "get_normalization_preview": get_normalization_preview,
        "get_merge_candidates": get_merge_candidates,
    },
    build_classification_detail_payload=build_classification_detail_payload,
)
register_library_entities_routes(
    app,
    state_provider=lambda: state,
    operations_provider=lambda: {
        "list_personalities": list_personalities,
        "get_personality_insights": get_personality_insights,
        "list_publishers": list_publishers,
        "get_publisher_insights": get_publisher_insights,
        "list_library_collections": list_library_collections,
        "get_collection_insights": get_collection_insights,
        "list_collection_items": list_collection_items,
        "update_collection": update_collection,
    },
    build_personality_payload=build_personality_payload,
    build_publisher_payload=build_publisher_payload,
    build_collections_payload=build_collections_payload,
)
register_core_read_routes(
    app,
    payload_provider=lambda: {
        "build_dashboard_payload": build_dashboard_payload,
        "build_schedules_payload": build_schedules_payload,
        "build_tasks_payload": build_tasks_payload,
        "build_task_detail_payload": build_task_detail_payload,
        "build_flow_detail_payload": build_flow_detail_payload,
        "build_library_payload": build_library_payload,
        "build_database_state_payload": build_database_state_payload,
    },
)
