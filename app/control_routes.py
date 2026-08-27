"""Control-plane API route registration for tasks and system actions."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.gemini_runtime import GeminiRuntimeManager
from app.gemini_workers import configured_gemini_account_count, validate_gemini_workers
from app.conveyor import (
    ConveyorEditConflict,
    ConveyorRevisionConflict,
    ConveyorValidationError,
)

def _parse_title(payload: Dict[str, Any], *, title_max_length: int, field_name: str = "title") -> str:
    value = payload.get(field_name)
    if value is None:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    title = str(value).strip()
    if not title:
        raise HTTPException(status_code=400, detail=f"{field_name} must be non-empty")
    if len(title) > title_max_length:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be at most {title_max_length} characters",
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


def _parse_integral(value: Any, *, field_name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or (
        isinstance(value, float) and not value.is_integer()
    ):
        raise HTTPException(status_code=400, detail=f"{field_name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field_name} must be an integer")
    if parsed < minimum:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be at least {minimum}",
        )
    return parsed


def register_control_routes(
    app: FastAPI,
    *,
    state_provider: Callable[[], Any],
    title_max_length: int,
) -> None:
    """Register task, conveyor, and system control endpoints."""

    @app.post("/api/tasks/{task_id}/toggle")
    def toggle_task(task_id: str, payload: Optional[Dict[str, Any]] = Body(default=None)) -> JSONResponse:
        """Start task or request stop/force-stop for active run."""
        state = state_provider()
        task = state.db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        sudo_password = _parse_optional_sudo_password(payload)
        result = state.runner.toggle_task(task_id, sudo_password=sudo_password)
        return JSONResponse(result)

    @app.patch("/api/tasks/{task_id}/title")
    def rename_task(task_id: str, payload: Dict[str, Any] = Body(...)) -> JSONResponse:
        """Rename one task definition."""
        state = state_provider()
        task = state.db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        title = _parse_title(payload, title_max_length=title_max_length)
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

    @app.patch("/api/tasks/{task_id}/gemini-workers")
    def set_gemini_workers(
        task_id: str, payload: Dict[str, Any] = Body(...)
    ) -> JSONResponse:
        """Set the one-shot worker count used by the next run."""
        state = state_provider()
        task = state.db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.get("gemini_workers_default") is None:
            raise HTTPException(status_code=400, detail="Task does not use Gemini workers")
        if state.db.get_active_run_for_task(task_id):
            raise HTTPException(status_code=409, detail="Worker count is locked while task is active")
        if "workers" not in payload:
            raise HTTPException(status_code=400, detail="workers is required")
        try:
            workers = validate_gemini_workers(
                payload["workers"], maximum=configured_gemini_account_count()
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        updated = state.db.set_task_gemini_workers_next(task_id, workers)
        if not updated:
            raise HTTPException(status_code=404, detail="Task not found")
        state.db.insert_event(
            "task.gemini_workers.updated",
            task_id=task_id,
            run_id=None,
            panel_id=task["panel_id"],
            payload={"workers": workers},
        )
        return JSONResponse({"workers": workers, "updated": True})

    @app.patch("/api/flows/{panel_id}/title")
    def rename_flow(panel_id: str, payload: Dict[str, Any] = Body(...)) -> JSONResponse:
        """Rename one dashboard flow (panel)."""
        state = state_provider()
        panel = state.db.get_panel(panel_id)
        if not panel:
            raise HTTPException(status_code=404, detail="Flow not found")

        title = _parse_title(payload, title_max_length=title_max_length)
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

    @app.put("/api/conveyor")
    def save_conveyor(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
        """Replace the singleton conveyor definition using optimistic revisioning."""
        if "revision" not in payload:
            raise HTTPException(status_code=400, detail="revision is required")
        if "stages" not in payload:
            raise HTTPException(status_code=400, detail="stages is required")
        state = state_provider()
        try:
            definition = state.conveyor_service.save_definition(
                expected_revision=_parse_integral(
                    payload["revision"],
                    field_name="revision",
                ),
                stages=payload["stages"],
            )
        except ConveyorValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (ConveyorRevisionConflict, ConveyorEditConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"definition": definition})

    @app.post("/api/conveyor/run")
    def run_conveyor(
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> JSONResponse:
        """Start the saved conveyor definition."""
        state = state_provider()
        return JSONResponse(
            state.conveyor_service.trigger(
                sudo_password=_parse_optional_sudo_password(payload),
            )
        )

    @app.post("/api/conveyor/stop")
    def stop_conveyor() -> JSONResponse:
        """Gracefully stop the active conveyor and cancel its pending rows."""
        state = state_provider()
        return JSONResponse(state.conveyor_service.stop())

    @app.post("/api/system/stop-all")
    def stop_all() -> JSONResponse:
        """Two-step global stop-all action: graceful, then force."""
        state = state_provider()
        result = state.runner.stop_all_toggle()
        return JSONResponse(result)

    @app.get("/api/gemini/state")
    def gemini_state() -> JSONResponse:
        """Return Gemini key/runtime snapshot."""
        state = state_provider()
        event_cursor = state.db.get_latest_event_id()
        manager = GeminiRuntimeManager(
            state.db,
            task_id=None,
            panel_id="library",
        )
        return JSONResponse(
            {
                "event_cursor": event_cursor,
                "gemini": manager.snapshot(),
            }
        )

    @app.post("/api/gemini/reset-key")
    def gemini_reset_key(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
        """Clear exhausted marker for one key across all models."""
        state = state_provider()
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
        state = state_provider()
        manager = GeminiRuntimeManager(
            state.db,
            task_id=None,
            panel_id="library",
        )
        changed = manager.reset_all()
        return JSONResponse({"ok": True, "rows_changed": changed})

    @app.post("/api/gemini/override-blackout")
    def gemini_override_blackout() -> JSONResponse:
        """Explicitly bypass only the currently active Gemini blackout."""
        state = state_provider()
        manager = GeminiRuntimeManager(
            state.db,
            task_id=None,
            panel_id="library",
        )
        try:
            result = manager.override_blackout()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"ok": True, **result})
