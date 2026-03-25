"""Manzara MVP API and dashboard UI server."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.db import Database
from app.gemini_runtime import GeminiRuntimeManager
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
from app.settings import Settings, load_settings
from app.tasks import TaskRunner
from app.workflows import WorkflowService

APP_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_ROOT / "static"
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
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
app = FastAPI(title="Manzara", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def on_startup() -> None:
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


@app.on_event("shutdown")
def on_shutdown() -> None:
    """Stop background scheduler worker on app shutdown."""
    state.shutting_down = True
    state.workflow_service.stop()


@app.get("/")
def index() -> RedirectResponse:
    """Redirect root to dashboard page."""
    return RedirectResponse(url="/dashboard", status_code=307)


@app.get("/dashboard")
def dashboard_page() -> FileResponse:
    """Serve dashboard page."""
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/schedules")
def schedules_page() -> FileResponse:
    """Serve schedules page."""
    return FileResponse(STATIC_DIR / "schedules.html")


@app.get("/tasks")
def tasks_page() -> FileResponse:
    """Serve task index page."""
    return FileResponse(STATIC_DIR / "tasks.html")


@app.get("/library")
def library_page() -> FileResponse:
    """Serve library insights page."""
    return FileResponse(STATIC_DIR / "library.html")


@app.get("/database")
def database_page() -> FileResponse:
    """Serve database diagnostics page."""
    return FileResponse(STATIC_DIR / "database.html")


@app.get("/gemini")
def gemini_page() -> FileResponse:
    """Serve Gemini key/runtime diagnostics page."""
    return FileResponse(STATIC_DIR / "gemini.html")


@app.get("/library/classifications")
def library_classifications_page() -> FileResponse:
    """Serve classifications control page."""
    return FileResponse(STATIC_DIR / "library-classifications.html")


@app.get("/library/classifications/{classification_id}")
def library_classification_detail_page(classification_id: int) -> FileResponse:
    """Serve one classification detail page shell."""
    _ = classification_id
    return FileResponse(STATIC_DIR / "library-classification.html")


@app.get("/library/personalities")
def library_personalities_page() -> FileResponse:
    """Serve personality control page."""
    return FileResponse(STATIC_DIR / "library-personalities.html")


@app.get("/library/publishers")
def library_publishers_page() -> FileResponse:
    """Serve publisher control page."""
    return FileResponse(STATIC_DIR / "library-publishers.html")


@app.get("/library/collections")
def library_collections_page() -> FileResponse:
    """Serve collections control page."""
    return FileResponse(STATIC_DIR / "library-collections.html")


@app.get("/library/normalization/{entity_type}")
def library_normalization_page(entity_type: str) -> FileResponse:
    """Serve normalization workbench page for personality/publisher."""
    if entity_type not in NORMALIZATION_ENTITY_TYPES:
        raise HTTPException(status_code=404, detail="Normalization entity type not found")
    return FileResponse(STATIC_DIR / "library-normalization.html")


@app.get("/tasks/{task_id:path}")
def task_detail_page(task_id: str) -> FileResponse:
    """Serve task detail page shell."""
    _ = task_id
    return FileResponse(STATIC_DIR / "task.html")


@app.get("/api/health")
def health() -> Dict[str, str]:
    """Simple health probe endpoint."""
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


def _parse_title(payload: Dict[str, Any], field_name: str = "title") -> str:
    value = payload.get(field_name)
    if value is None:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    title = str(value).strip()
    if not title:
        raise HTTPException(status_code=400, detail=f"{field_name} must be non-empty")
    if len(title) > _TITLE_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be at most {_TITLE_MAX_LENGTH} characters",
        )
    return title


def _parse_optional_sudo_password(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    """Parse optional sudo password from request body."""
    if not payload or "sudo_password" not in payload:
        return None
    raw_value = payload.get("sudo_password")
    if raw_value is None:
        return None
    value = str(raw_value)
    if value == "":
        return None
    if len(value) > 1024:
        raise HTTPException(status_code=400, detail="sudo_password is too long")
    return value


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


def build_dashboard_payload() -> Dict[str, Any]:
    """Compose dashboard payload from DB and Shayan artifacts."""
    panel_titles = state.db.get_panel_title_map()
    task_slug_map, _ = _task_slug_maps()

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
                },
            }
        )

    workflows = state.db.list_workflows_with_latest_run()
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

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": {
            "active_tasks": len(active_runs),
            "active_workflows": active_workflow_runs,
            "failed_runs": len([r for r in state.db.list_recent_runs(50) if r["status"] == "failed"]),
            "stop_all_state": stop_all_state,
        },
        "panels": [shayan_panel, maintenance_panel, oscar_panel, library_panel],
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
    tasks = state.db.list_tasks_with_latest_run()
    task_groups: Dict[str, Dict[str, Any]] = {}
    for task in tasks:
        panel_id = str(task["panel_id"])
        group = task_groups.setdefault(
            panel_id,
            {
                "panel_id": panel_id,
                "title": panel_titles.get(panel_id, panel_id),
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


def build_task_detail_payload(task_key: str, limit: int = 100) -> Dict[str, Any]:
    """Compose one task detail payload with run history."""
    task = _resolve_task_identifier(task_key)
    task_slug_map, _ = _task_slug_maps()
    task_id = str(task["task_id"])

    panel = state.db.get_panel(str(task["panel_id"])) or {
        "panel_id": task["panel_id"],
        "title": str(task["panel_id"]),
    }
    runs = state.db.list_recent_runs_for_task(task_id, limit=limit)
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
    limit: int = Query(100, ge=1, le=400),
) -> JSONResponse:
    """Return one task with run history (task id or slug)."""
    return JSONResponse(build_task_detail_payload(task_id, limit=limit))


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


@app.post("/api/tasks/{task_id}/toggle")
def toggle_task(task_id: str, payload: Optional[Dict[str, Any]] = Body(default=None)) -> JSONResponse:
    """Start task or request stop/force-stop for active run."""
    task = state.db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    sudo_password = _parse_optional_sudo_password(payload)
    result = state.runner.toggle_task(task_id, sudo_password=sudo_password)
    return JSONResponse(result)


@app.patch("/api/tasks/{task_id}/title")
def rename_task(task_id: str, payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    """Rename one task definition."""
    task = state.db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    title = _parse_title(payload)
    if title == task["title"]:
        return JSONResponse({"task": task, "updated": False})

    updated = state.db.update_task_title(task_id, title)
    if not updated:
        raise HTTPException(status_code=404, detail="Task not found")

    state.db.insert_event(
        "task.renamed",
        task_id=task_id,
        run_id=None,
        panel_id=task["panel_id"],
        payload={"old_title": task["title"], "new_title": title},
    )
    return JSONResponse({"task": updated, "updated": True})


@app.patch("/api/flows/{panel_id}/title")
def rename_flow(panel_id: str, payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    """Rename one dashboard flow (panel)."""
    panel = state.db.get_panel(panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="Flow not found")

    title = _parse_title(payload)
    if title == panel["title"]:
        return JSONResponse({"flow": panel, "updated": False})

    updated = state.db.update_panel_title(panel_id, title)
    if not updated:
        raise HTTPException(status_code=404, detail="Flow not found")

    state.db.insert_event(
        "flow.renamed",
        task_id=None,
        run_id=None,
        panel_id=panel_id,
        payload={"old_title": panel["title"], "new_title": title},
    )
    return JSONResponse({"flow": updated, "updated": True})


@app.post("/api/workflows/{workflow_id}/run")
def run_workflow_now(
    workflow_id: str,
    payload: Optional[Dict[str, Any]] = Body(default=None),
) -> JSONResponse:
    """Trigger one workflow immediately."""
    workflow = state.db.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    sudo_password = _parse_optional_sudo_password(payload)
    result = state.workflow_service.trigger_workflow(
        workflow_id,
        trigger_source="manual",
        sudo_password=sudo_password,
    )
    return JSONResponse(result)


@app.get("/api/workflows/{workflow_id}")
def get_workflow(workflow_id: str) -> JSONResponse:
    """Get one workflow with schedule and steps."""
    workflow = state.db.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    return JSONResponse(
        {
            "workflow": workflow,
            "schedule": state.db.get_schedule_by_workflow(workflow_id),
            "steps": state.db.list_workflow_steps(workflow_id),
            "recent_runs": state.db.list_recent_workflow_runs(workflow_id, limit=10),
        }
    )


@app.patch("/api/schedules/{schedule_id}")
def update_schedule(schedule_id: str, payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    """Patch schedule config and recalculate next run time."""
    schedule = state.db.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    updates: Dict[str, Any] = {}
    schedule_type = str(schedule.get("schedule_type") or "weekly")

    if "schedule_type" in payload:
        parsed = str(payload["schedule_type"]).strip().lower()
        if parsed not in {"weekly", "interval"}:
            raise HTTPException(status_code=400, detail="schedule_type must be 'weekly' or 'interval'")
        updates["schedule_type"] = parsed
        schedule_type = parsed

    if "enabled" in payload:
        raw_enabled = payload["enabled"]
        if isinstance(raw_enabled, bool):
            updates["enabled"] = raw_enabled
        elif isinstance(raw_enabled, (int, float)):
            updates["enabled"] = bool(raw_enabled)
        elif isinstance(raw_enabled, str):
            updates["enabled"] = raw_enabled.strip().lower() in {"1", "true", "yes", "on"}
        else:
            raise HTTPException(status_code=400, detail="enabled must be a boolean-like value")

    if "day_of_week" in payload:
        try:
            day = int(payload["day_of_week"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="day_of_week must be an integer 1..7")
        if day < 1 or day > 7:
            raise HTTPException(status_code=400, detail="day_of_week must be an integer 1..7")
        updates["day_of_week"] = day

    if "time_of_day" in payload:
        value = str(payload["time_of_day"]).strip()
        if not _TIME_PATTERN.match(value):
            raise HTTPException(status_code=400, detail="time_of_day must match HH:MM (24h)")
        updates["time_of_day"] = value

    if "interval_minutes" in payload:
        raw_interval = payload["interval_minutes"]
        if raw_interval in {None, ""}:
            updates["interval_minutes"] = None
        else:
            try:
                interval_minutes = int(raw_interval)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="interval_minutes must be an integer >= 1")
            if interval_minutes < 1:
                raise HTTPException(status_code=400, detail="interval_minutes must be an integer >= 1")
            updates["interval_minutes"] = interval_minutes

    if "timezone" in payload:
        timezone_name = str(payload["timezone"]).strip() or "UTC"
        updates["timezone"] = timezone_name

    if schedule_type == "interval":
        effective_interval = updates.get("interval_minutes", schedule.get("interval_minutes"))
        if effective_interval is None:
            raise HTTPException(
                status_code=400,
                detail="interval_minutes is required when schedule_type is 'interval'",
            )

    if not updates:
        return JSONResponse({"schedule": schedule})

    updated = state.workflow_service.configure_schedule(schedule_id, updates)
    if not updated:
        raise HTTPException(status_code=404, detail="Schedule not found")

    return JSONResponse({"schedule": updated})


@app.post("/api/system/stop-all")
def stop_all() -> JSONResponse:
    """Two-step global stop-all action: graceful, then force."""
    result = state.runner.stop_all_toggle()
    return JSONResponse(result)


@app.get("/api/gemini/state")
def gemini_state() -> JSONResponse:
    """Return Gemini key/runtime snapshot."""
    manager = GeminiRuntimeManager(
        state.db,
        task_id=None,
        panel_id="library",
    )
    return JSONResponse({"gemini": manager.snapshot()})


@app.post("/api/gemini/reset-key")
def gemini_reset_key(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    """Clear exhausted marker for one key across all models."""
    key_id = str(payload.get("key_id") or "").strip()
    if not key_id:
        raise HTTPException(status_code=400, detail="key_id is required")
    manager = GeminiRuntimeManager(
        state.db,
        task_id=None,
        panel_id="library",
    )
    changed = manager.reset_key(key_id)
    return JSONResponse({"ok": True, "key_id": key_id, "rows_changed": changed})


@app.post("/api/gemini/reset-all")
def gemini_reset_all() -> JSONResponse:
    """Clear exhausted marker for all keys and models."""
    manager = GeminiRuntimeManager(
        state.db,
        task_id=None,
        panel_id="library",
    )
    changed = manager.reset_all()
    return JSONResponse({"ok": True, "rows_changed": changed})


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
