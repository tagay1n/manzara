"""Manzara MVP API and dashboard UI server."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.db import Database
from app.modules.shayan.panel import build_shayan_panel
from app.modules.shayan.tasks import shayan_task_definitions
from app.settings import Settings, load_settings
from app.tasks import TaskRunner

APP_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_ROOT / "static"


class AppState:
    """Typed state holder for shared app services."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.runner = TaskRunner(self.db)


settings = load_settings()
state = AppState(settings)
app = FastAPI(title="Manzara", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def on_startup() -> None:
    """Initialize schema and seed known task definitions."""
    state.db.init_schema()
    state.db.seed_tasks(shayan_task_definitions(state.settings.shayan))
    recovered = state.db.recover_active_runs()
    if recovered > 0:
        state.db.insert_event(
            "system.recovery",
            task_id=None,
            run_id=None,
            panel_id=None,
            payload={"recovered_runs": recovered},
        )


@app.get("/")
def index() -> FileResponse:
    """Serve dashboard entrypoint."""
    return FileResponse(STATIC_DIR / "index.html")


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

    shayan_panel = build_shayan_panel(
        db=state.db,
        shayan=state.settings.shayan,
        tasks=tasks_by_panel.get("shayan", []),
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
            "failed_runs": len([r for r in state.db.list_recent_runs(50) if r["status"] == "failed"]),
            "stop_all_state": stop_all_state,
        },
        "panels": [shayan_panel],
        "recent_runs": state.db.list_recent_runs(20),
    }


@app.get("/api/dashboard")
def get_dashboard() -> JSONResponse:
    """Return current dashboard state."""
    return JSONResponse(build_dashboard_payload())


@app.post("/api/tasks/{task_id}/toggle")
def toggle_task(task_id: str) -> JSONResponse:
    """Start task or request stop/force-stop for active run."""
    task = state.db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    result = state.runner.toggle_task(task_id)
    return JSONResponse(result)


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
