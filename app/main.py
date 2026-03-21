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
from app.modules.maintenance.panel import build_library_panel, build_maintenance_panel
from app.modules.maintenance.tasks import maintenance_task_definitions
from app.modules.maintenance.workflow import library_workflow_bundle
from app.modules.shayan.panel import build_shayan_panel
from app.modules.shayan.tasks import shayan_task_definitions
from app.modules.shayan.workflow import (
    shayan_workflow_bundle,
)
from app.settings import Settings, load_settings
from app.tasks import TaskRunner
from app.workflows import WorkflowService

APP_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_ROOT / "static"
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class AppState:
    """Typed state holder for shared app services."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.runner = TaskRunner(self.db)
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
    state.db.init_schema()
    task_defs = [
        *shayan_task_definitions(state.settings.shayan),
        *maintenance_task_definitions(state.settings.maintenance),
    ]
    state.db.seed_tasks(task_defs)
    state.db.seed_workflow_bundle(shayan_workflow_bundle(state.settings.shayan))
    state.db.seed_workflow_bundle(library_workflow_bundle())

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


@app.get("/api/health")
def health() -> Dict[str, str]:
    """Simple health probe endpoint."""
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


def build_dashboard_payload() -> Dict[str, Any]:
    """Compose dashboard payload from DB and Shayan artifacts."""
    tasks = state.db.list_tasks_with_latest_run()
    tasks_by_panel: Dict[str, list[Dict[str, Any]]] = {}
    for task in tasks:
        panel_id = task["panel_id"]
        tasks_by_panel.setdefault(panel_id, []).append(
            {
                "task_id": task["task_id"],
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
    )
    maintenance_panel = build_maintenance_panel(
        db=state.db,
        maintenance=state.settings.maintenance,
        tasks=tasks_by_panel.get("maintenance", []),
    )
    library_panel = build_library_panel(
        db=state.db,
        maintenance=state.settings.maintenance,
        tasks=tasks_by_panel.get("library", []),
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

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": {
            "active_tasks": len(active_runs),
            "active_workflows": active_workflow_runs,
            "failed_runs": len([r for r in state.db.list_recent_runs(50) if r["status"] == "failed"]),
            "stop_all_state": stop_all_state,
        },
        "panels": [shayan_panel, maintenance_panel, library_panel],
        "recent_runs": state.db.list_recent_runs(20),
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


@app.get("/api/dashboard")
def get_dashboard() -> JSONResponse:
    """Return current dashboard state."""
    return JSONResponse(build_dashboard_payload())


@app.get("/api/schedules")
def get_schedules() -> JSONResponse:
    """Return workflows and schedule configuration state."""
    return JSONResponse(build_schedules_payload())


@app.post("/api/tasks/{task_id}/toggle")
def toggle_task(task_id: str) -> JSONResponse:
    """Start task or request stop/force-stop for active run."""
    task = state.db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    result = state.runner.toggle_task(task_id)
    return JSONResponse(result)


@app.post("/api/workflows/{workflow_id}/run")
def run_workflow_now(workflow_id: str) -> JSONResponse:
    """Trigger one workflow immediately."""
    workflow = state.db.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    result = state.workflow_service.trigger_workflow(
        workflow_id,
        trigger_source="manual",
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

    if "timezone" in payload:
        timezone_name = str(payload["timezone"]).strip() or "UTC"
        updates["timezone"] = timezone_name

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


@app.get("/api/runs/{run_id}/logs")
def run_logs(
    run_id: int,
    after_log_id: int = Query(0, ge=0),
    limit: int = Query(400, ge=1, le=2000),
) -> JSONResponse:
    """Return logs for one run with incremental pagination."""
    run = state.db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    lines = state.db.get_logs(run_id=run_id, after_log_id=after_log_id, limit=limit)
    return JSONResponse(
        {
            "run": run,
            "lines": lines,
            "next_after_log_id": lines[-1]["log_id"] if lines else after_log_id,
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
        while True:
            if await request.is_disconnected():
                break

            events = state.db.get_events_after(cursor, limit=200)
            if events:
                for event in events:
                    cursor = int(event["event_id"])
                    data = json.dumps(event, ensure_ascii=False)
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {data}\n\n"
                heartbeat_counter = 0
            else:
                heartbeat_counter += 1
                if heartbeat_counter >= 15:
                    yield ": heartbeat\n\n"
                    heartbeat_counter = 0
                await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
