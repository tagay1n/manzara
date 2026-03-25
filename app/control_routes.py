"""Control-plane API route registration for tasks/workflows/system actions."""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.gemini_runtime import GeminiRuntimeManager

_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


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


def register_control_routes(
    app: FastAPI,
    *,
    state_provider: Callable[[], Any],
    title_max_length: int,
) -> None:
    """Register task/workflow/schedule/system control endpoints."""

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

    @app.post("/api/workflows/{workflow_id}/run")
    def run_workflow_now(
        workflow_id: str,
        payload: Optional[Dict[str, Any]] = Body(default=None),
    ) -> JSONResponse:
        """Trigger one workflow immediately."""
        state = state_provider()
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
        state = state_provider()
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
        state = state_provider()
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
                normalized = raw_enabled.strip().lower()
                if normalized in {"1", "true", "yes", "on"}:
                    updates["enabled"] = True
                elif normalized in {"0", "false", "no", "off"}:
                    updates["enabled"] = False
                else:
                    raise HTTPException(status_code=400, detail="enabled must be a boolean-like value")
            else:
                raise HTTPException(status_code=400, detail="enabled must be a boolean-like value")

        if "day_of_week" in payload:
            raw_day = payload["day_of_week"]
            if isinstance(raw_day, bool) or (
                isinstance(raw_day, float) and not raw_day.is_integer()
            ):
                raise HTTPException(status_code=400, detail="day_of_week must be an integer 1..7")
            try:
                day = int(raw_day)
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
                if isinstance(raw_interval, bool) or (
                    isinstance(raw_interval, float) and not raw_interval.is_integer()
                ):
                    raise HTTPException(status_code=400, detail="interval_minutes must be an integer >= 1")
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
        state = state_provider()
        result = state.runner.stop_all_toggle()
        return JSONResponse(result)

    @app.get("/api/gemini/state")
    def gemini_state() -> JSONResponse:
        """Return Gemini key/runtime snapshot."""
        state = state_provider()
        manager = GeminiRuntimeManager(
            state.db,
            task_id=None,
            panel_id="library",
        )
        return JSONResponse({"gemini": manager.snapshot()})

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
