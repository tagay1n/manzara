"""Application route/setup wiring orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from fastapi import FastAPI

from app.contracts import (
    ClassificationOperationsProvider,
    EntitiesOperationsProvider,
    NormalizationOperationsProvider,
    RoutePayloadBuildersProvider,
    StateProvider,
    StreamRouteHandlers,
)
from app.dependencies import build_core_read_payload_builders
from app.control_routes import register_control_routes
from app.core_read_routes import register_core_read_routes
from app.library_classification_routes import register_library_classification_routes
from app.library_entities_routes import register_library_entities_routes
from app.library_normalization_routes import register_library_normalization_routes
from app.page_routes import register_page_routes
from app.stream_routes import register_stream_routes


def register_app_routes(
    app: FastAPI,
    *,
    state_provider: StateProvider,
    static_dir: Path,
    normalization_entity_types: Iterable[str],
    title_max_length: int,
    sse_poll_interval_seconds: float,
    sse_heartbeat_every_empty_polls: int,
    payload_provider: RoutePayloadBuildersProvider,
    normalization_operations_provider: NormalizationOperationsProvider,
    classification_operations_provider: ClassificationOperationsProvider,
    entities_operations_provider: EntitiesOperationsProvider,
) -> StreamRouteHandlers:
    """Register all application routes and return stream route handlers."""
    register_page_routes(
        app,
        static_dir=static_dir,
        normalization_entity_types=normalization_entity_types,
    )
    register_control_routes(
        app,
        state_provider=state_provider,
        title_max_length=title_max_length,
    )
    stream_route_handlers = register_stream_routes(
        app,
        state_provider=state_provider,
        sse_poll_interval_seconds=sse_poll_interval_seconds,
        sse_heartbeat_every_empty_polls=sse_heartbeat_every_empty_polls,
    )
    register_library_normalization_routes(
        app,
        state_provider=state_provider,
        normalization_entity_types=normalization_entity_types,
        build_normalization_payload=lambda entity_type: payload_provider().build_normalization_payload(
            entity_type
        ),
        operations_provider=normalization_operations_provider,
    )
    register_library_classification_routes(
        app,
        operations_provider=classification_operations_provider,
        build_classification_detail_payload=lambda classification_id, **kwargs: payload_provider().build_classification_detail_payload(
            classification_id, **kwargs
        ),
    )
    register_library_entities_routes(
        app,
        state_provider=state_provider,
        operations_provider=entities_operations_provider,
        build_personality_payload=lambda: payload_provider().build_personality_payload(),
        build_publisher_payload=lambda: payload_provider().build_publisher_payload(),
        build_collections_payload=lambda: payload_provider().build_collections_payload(),
    )
    register_core_read_routes(
        app,
        payload_provider=lambda: build_core_read_payload_builders(payload_provider()),
    )
    return stream_route_handlers
