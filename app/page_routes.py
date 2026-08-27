"""Static/UI page route registration."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse


def _page_response(target: Path) -> FileResponse:
    """Serve application HTML without retaining stale shell markup in browsers."""
    return FileResponse(target, headers={"Cache-Control": "no-store"})


def register_page_routes(
    app: FastAPI,
    *,
    static_dir: Path,
    normalization_entity_types: Iterable[str],
) -> None:
    """Register all non-API page routes."""
    allowed_normalization = {str(item) for item in normalization_entity_types}

    async def _index(_request: Request) -> RedirectResponse:
        return RedirectResponse(url="/tasks", status_code=307)

    app.add_api_route("/", _index, methods=["GET"])

    async def _dashboard_redirect(_request: Request) -> RedirectResponse:
        return RedirectResponse(url="/tasks", status_code=307)

    app.add_api_route("/dashboard", _dashboard_redirect, methods=["GET"])

    routes = [
        ("/tasks", "tasks.html"),
        ("/library", "library.html"),
        ("/database", "database.html"),
        ("/gemini", "gemini.html"),
        ("/library/classifications", "library-classifications.html"),
        ("/library/personalities", "library-personalities.html"),
        ("/library/publishers", "library-publishers.html"),
        ("/library/collections", "library-collections.html"),
        ("/library/document-cleanup", "library-document-cleanup.html"),
    ]

    for path, file_name in routes:
        target = static_dir / file_name

        async def _serve(_request: Request, _target: Path = target) -> FileResponse:
            return _page_response(_target)

        app.add_api_route(path, _serve, methods=["GET"])

    async def _classification_detail(_request: Request, classification_id: int) -> FileResponse:
        _ = classification_id
        return _page_response(static_dir / "library-classification.html")

    app.add_api_route(
        "/library/classifications/{classification_id}",
        _classification_detail,
        methods=["GET"],
    )

    async def _normalization_page(_request: Request, entity_type: str) -> FileResponse:
        if entity_type not in allowed_normalization:
            raise HTTPException(status_code=404, detail="Normalization entity type not found")
        return _page_response(static_dir / "library-normalization.html")

    app.add_api_route("/library/normalization/{entity_type}", _normalization_page, methods=["GET"])

    async def _task_detail(_request: Request, task_id: str) -> FileResponse:
        _ = task_id
        return _page_response(static_dir / "task.html")

    app.add_api_route("/tasks/{task_id:path}", _task_detail, methods=["GET"])
