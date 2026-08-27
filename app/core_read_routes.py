"""Core read-only API route registration."""

from __future__ import annotations

from typing import Callable

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from app.contracts import CoreReadPayloadBuilders


def register_core_read_routes(
    app: FastAPI,
    *,
    payload_provider: Callable[[], CoreReadPayloadBuilders],
) -> None:
    """Register core read-only endpoints backed by payload builder functions."""

    @app.get("/api/system/state")
    def get_system_state() -> JSONResponse:
        """Return lightweight global operational state."""
        payloads = payload_provider()
        return JSONResponse(payloads.build_system_state_payload())

    @app.get("/api/dashboard")
    def get_dashboard() -> JSONResponse:
        """Return current dashboard state."""
        payloads = payload_provider()
        return JSONResponse(payloads.build_dashboard_payload())

    @app.get("/api/tasks")
    def get_tasks() -> JSONResponse:
        """Return all tasks grouped by flow."""
        payloads = payload_provider()
        return JSONResponse(payloads.build_tasks_payload())

    @app.get("/api/tasks/{task_id}")
    def get_task_detail(
        task_id: str,
        limit: int = Query(20, ge=1, le=400),
    ) -> JSONResponse:
        """Return one task with run history (task id or slug)."""
        payloads = payload_provider()
        return JSONResponse(payloads.build_task_detail_payload(task_id, limit=limit))

    @app.get("/api/library")
    def get_library() -> JSONResponse:
        """Return library applicability dataset statistics."""
        payloads = payload_provider()
        return JSONResponse(payloads.build_library_payload())

    @app.get("/api/database/state")
    def get_database_state() -> JSONResponse:
        """Return database diagnostics snapshot."""
        payloads = payload_provider()
        return JSONResponse(payloads.build_database_state_payload())
