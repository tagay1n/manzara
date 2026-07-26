"""FastAPI application factory for Manzara."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncContextManager, Callable, Iterable

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from app.app_setup import register_app_routes
from app.contracts import (
    ClassificationOperationsProvider,
    EntitiesOperationsProvider,
    NormalizationOperationsProvider,
    RoutePayloadBuildersProvider,
    StateProvider,
    StreamRouteHandlers,
)


@dataclass(frozen=True)
class AppFactoryResult:
    """Built FastAPI app plus extracted stream-route handlers."""

    app: FastAPI
    stream_handlers: StreamRouteHandlers


class RevalidatingStaticFiles(StaticFiles):
    """Let browsers cache UI assets only after validating their current ETag."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "no-cache"
        return response


def create_manzara_app(
    *,
    static_dir: Path,
    lifespan: Callable[[FastAPI], AsyncContextManager[Any]],
    state_provider: StateProvider,
    normalization_entity_types: Iterable[str],
    title_max_length: int,
    sse_poll_interval_seconds: float,
    sse_heartbeat_every_empty_polls: int,
    payload_provider: RoutePayloadBuildersProvider,
    normalization_operations_provider: NormalizationOperationsProvider,
    classification_operations_provider: ClassificationOperationsProvider,
    entities_operations_provider: EntitiesOperationsProvider,
) -> AppFactoryResult:
    """Create and fully wire the FastAPI app."""
    app = FastAPI(title="Manzara", version="0.1.0", lifespan=lifespan)
    app.mount("/static", RevalidatingStaticFiles(directory=static_dir), name="static")
    stream_handlers = register_app_routes(
        app,
        state_provider=state_provider,
        static_dir=static_dir,
        normalization_entity_types=normalization_entity_types,
        title_max_length=title_max_length,
        sse_poll_interval_seconds=sse_poll_interval_seconds,
        sse_heartbeat_every_empty_polls=sse_heartbeat_every_empty_polls,
        payload_provider=payload_provider,
        normalization_operations_provider=normalization_operations_provider,
        classification_operations_provider=classification_operations_provider,
        entities_operations_provider=entities_operations_provider,
    )
    return AppFactoryResult(app=app, stream_handlers=stream_handlers)
