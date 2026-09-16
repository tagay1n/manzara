"""Manzara MVP API and dashboard UI server."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import FastAPI

from app.attention import AttentionService
from app.attention_registry import build_attention_providers
from app.bootstrap import shutdown_app, startup_app
from app.constants import (
    PANEL_DEFS,
    SLUG_CLEAN_PATTERN,
    SLUG_SEPARATOR_PATTERN,
    SSE_HEARTBEAT_EVERY_EMPTY_POLLS,
    SSE_POLL_INTERVAL_SECONDS,
    STATIC_DIR,
    TITLE_MAX_LENGTH,
)
from app.conveyor import ConveyorService
from app.db import Database
from app.dependencies import (
    ApplicationOperations,
    build_application_operations,
    build_route_payload_builders,
)
from app.factory import create_manzara_app
from app.modules.library.collection_tasks import collection_task_definitions
from app.modules.library.normalization import ENTITY_TYPES as NORMALIZATION_ENTITY_TYPES
from app.modules.library.tasks import library_task_definitions
from app.modules.maintenance.tasks import maintenance_task_definitions
from app.payload_builder import PayloadBuilder
from app.registry import build_startup_seed_registry
from app.settings import Settings, load_settings
from app.tasks import TaskRunner

# Backward-compatible aliases used by tests and legacy references.
_PANEL_DEFS = PANEL_DEFS
_SSE_POLL_INTERVAL_SECONDS = SSE_POLL_INTERVAL_SECONDS
_SSE_HEARTBEAT_EVERY_EMPTY_POLLS = SSE_HEARTBEAT_EVERY_EMPTY_POLLS
_TITLE_MAX_LENGTH = TITLE_MAX_LENGTH
_SLUG_SEPARATOR_PATTERN = SLUG_SEPARATOR_PATTERN
_SLUG_CLEAN_PATTERN = SLUG_CLEAN_PATTERN


class AppState:
    """Typed state holder for shared app services."""

    def __init__(
        self, settings: Settings, *, operations: ApplicationOperations | None = None
    ):
        self.settings = settings
        self.operations = (
            operations if operations is not None else build_application_operations()
        )
        self.db = Database(
            settings.database_url,
            schema=settings.database_schema,
            pool_size=settings.database_pool_size,
            local_state_path=settings.local_state_path,
        )
        self.runner = TaskRunner(self.db)
        self.shutting_down = False
        self.conveyor_service = ConveyorService(self.db, self.runner)
        self.attention_service = AttentionService(
            self.db, build_attention_providers(settings)
        )


settings = load_settings()
state = AppState(settings)
payload_builder = PayloadBuilder(
    state_provider=lambda: state,
    normalization_entity_types=NORMALIZATION_ENTITY_TYPES,
    slug_separator_pattern=_SLUG_SEPARATOR_PATTERN,
    slug_clean_pattern=_SLUG_CLEAN_PATTERN,
    ops_provider=lambda: state.operations.payload,
)


def _build_startup_registry() -> Dict[str, list[Dict[str, Any]]]:
    registry = build_startup_seed_registry(
        state.settings,
        panel_defs=_PANEL_DEFS,
        maintenance_task_definitions=maintenance_task_definitions,
        library_task_definitions=library_task_definitions,
        collection_task_definitions=collection_task_definitions,
    )
    return {
        "panel_defs": registry.panel_defs,
        "task_defs": registry.task_defs,
    }


def _startup() -> None:
    """Initialize schema and seed known panel and task definitions."""
    registry = _build_startup_registry()
    startup_app(
        state=state,
        panel_defs=registry["panel_defs"],
        task_defs=registry["task_defs"],
    )


def _shutdown() -> None:
    """Mark application state as shutting down."""
    shutdown_app(state=state)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """FastAPI lifespan hook for startup/shutdown orchestration."""
    _startup()
    try:
        yield
    finally:
        _shutdown()


_factory_result = create_manzara_app(
    static_dir=STATIC_DIR,
    lifespan=_lifespan,
    state_provider=lambda: state,
    normalization_entity_types=NORMALIZATION_ENTITY_TYPES,
    title_max_length=_TITLE_MAX_LENGTH,
    sse_poll_interval_seconds=_SSE_POLL_INTERVAL_SECONDS,
    sse_heartbeat_every_empty_polls=_SSE_HEARTBEAT_EVERY_EMPTY_POLLS,
    payload_provider=lambda: build_route_payload_builders(payload_builder),
    normalization_operations_provider=lambda: state.operations.normalization,
    classification_operations_provider=lambda: state.operations.classification,
    entities_operations_provider=lambda: state.operations.entities,
)
app = _factory_result.app
run_logs = _factory_result.stream_handlers["run_logs"]
events_stream = _factory_result.stream_handlers["events_stream"]


@app.get("/api/health")
def health() -> Dict[str, str]:
    """Simple health probe endpoint."""
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}
