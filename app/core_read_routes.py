"""Core read-only API route registration."""

from __future__ import annotations

from typing import Any, Callable, Dict

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse


def register_core_read_routes(
    app: FastAPI,
    *,
    payload_provider: Callable[[], Dict[str, Callable[..., Dict[str, Any]]]],
) -> None:
    """Register core read-only endpoints backed by payload builder functions."""

    @app.get("/api/dashboard")
    def get_dashboard() -> JSONResponse:
        """Return current dashboard state."""
        build = payload_provider()["build_dashboard_payload"]
        return JSONResponse(build())

    @app.get("/api/schedules")
    def get_schedules() -> JSONResponse:
        """Return workflows and schedule configuration state."""
        build = payload_provider()["build_schedules_payload"]
        return JSONResponse(build())

    @app.get("/api/tasks")
    def get_tasks() -> JSONResponse:
        """Return all tasks grouped by flow."""
        build = payload_provider()["build_tasks_payload"]
        return JSONResponse(build())

    @app.get("/api/tasks/{task_id}")
    def get_task_detail(
        task_id: str,
        limit: int = Query(20, ge=1, le=400),
    ) -> JSONResponse:
        """Return one task with run history (task id or slug)."""
        build = payload_provider()["build_task_detail_payload"]
        return JSONResponse(build(task_id, limit=limit))

    @app.get("/api/flows/{flow_id_or_slug}")
    def get_flow_detail(
        flow_id_or_slug: str,
        limit_per_task: int = Query(20, ge=1, le=200),
    ) -> JSONResponse:
        """Return one flow with panel stats and per-task run history."""
        build = payload_provider()["build_flow_detail_payload"]
        return JSONResponse(build(flow_id_or_slug, limit_per_task=limit_per_task))

    @app.get("/api/library")
    def get_library() -> JSONResponse:
        """Return library applicability dataset statistics."""
        build = payload_provider()["build_library_payload"]
        return JSONResponse(build())

    @app.get("/api/database/state")
    def get_database_state() -> JSONResponse:
        """Return database diagnostics snapshot."""
        build = payload_provider()["build_database_state_payload"]
        return JSONResponse(build())
