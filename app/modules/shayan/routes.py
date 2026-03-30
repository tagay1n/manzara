"""Shayan-specific API routes."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.modules.shayan.catalog import build_shayan_catalog, request_episode_redownload


def register_shayan_routes(
    app: FastAPI,
    *,
    state_provider: Callable[[], Any],
) -> None:
    """Register Shayan catalog and per-episode control routes."""

    @app.get("/api/shayan/catalog")
    def get_shayan_catalog() -> JSONResponse:
        state = state_provider()
        payload = build_shayan_catalog(
            state.db,
            output_path=state.settings.shayan.output_path,
        )
        return JSONResponse(payload)

    @app.post("/api/shayan/episodes/{entry_key}/redownload")
    def post_shayan_episode_redownload(entry_key: str) -> JSONResponse:
        state = state_provider()
        try:
            payload = request_episode_redownload(
                db=state.db,
                output_path=state.settings.shayan.output_path,
                runner=state.runner,
                entry_key=entry_key,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

