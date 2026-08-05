"""Manzara MVP API and dashboard UI server."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import FastAPI

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
from app.contracts import (
    ClassificationOperations,
    EntitiesOperations,
    NormalizationOperations,
    PayloadBuilderOperations,
)
from app.db import Database
from app.dependencies import (
    build_classification_operations_with_overrides,
    build_entities_operations_with_overrides,
    build_normalization_operations_with_overrides,
    build_payload_builder_operations_with_overrides,
    build_route_payload_builders,
)
from app.factory import create_manzara_app
from app.modules.library.collections import (
    get_collection_insights,
    get_collection_overview,
    get_collection_review,
    list_collection_items,
    list_collections as list_library_collections,
    update_collection,
)
from app.modules.library.insights import (
    get_classification_detail,
    get_classification_insights,
    get_merge_candidates,
    merge_classifications,
    get_normalization_preview,
    list_classifications,
)
from app.modules.library.normalization import (
    ENTITY_TYPES as NORMALIZATION_ENTITY_TYPES,
    bulk_link_aliases,
    bulk_reject_aliases,
    create_and_link_alias,
    create_canonical,
    get_evidence as get_normalization_evidence,
    get_merge_candidates as get_normalization_merge_candidates,
    get_normalization_dashboard,
    get_quality as get_normalization_quality,
    get_review_queue,
    link_alias,
    list_canonicals,
    list_history as list_normalization_history,
    list_suggestions,
    merge_canonicals,
    refresh_suggestions,
    reject_alias,
    undo_event,
)
from app.modules.library.personalities import (
    get_personality_insights,
    get_personality_overview,
    list_personalities,
)
from app.modules.library.publishers import (
    get_publisher_insights,
    get_publisher_overview,
    list_publishers,
)
from app.modules.library.stats import get_library_dataset_stats
from app.modules.library.tasks import library_task_definitions
from app.modules.maintenance.panel import (
    build_database_state_snapshot,
    build_library_panel,
    build_maintenance_panel,
)
from app.modules.maintenance.tasks import maintenance_task_definitions
from app.modules.maintenance.workflow import (
    library_personality_normalization_workflow_bundle,
    library_publisher_normalization_workflow_bundle,
    library_workflow_bundle,
    maintenance_backup_full_workflow_bundle,
    maintenance_backup_incr_workflow_bundle,
)
from app.modules.shayan.panel import build_shayan_panel
from app.modules.shayan.tasks import shayan_task_definitions
from app.modules.shayan.workflow import shayan_workflow_bundle
from app.payload_builder import PayloadBuilder
from app.registry import build_startup_seed_registry
from app.run_summary import build_default_run_summary
from app.settings import Settings, load_settings
from app.tasks import TaskRunner
from app.workflows import WorkflowService

# Backward-compatible aliases used by tests and legacy references.
_PANEL_DEFS = PANEL_DEFS
_SSE_POLL_INTERVAL_SECONDS = SSE_POLL_INTERVAL_SECONDS
_SSE_HEARTBEAT_EVERY_EMPTY_POLLS = SSE_HEARTBEAT_EVERY_EMPTY_POLLS
_TITLE_MAX_LENGTH = TITLE_MAX_LENGTH
_SLUG_SEPARATOR_PATTERN = SLUG_SEPARATOR_PATTERN
_SLUG_CLEAN_PATTERN = SLUG_CLEAN_PATTERN


def _payload_builder_operations() -> PayloadBuilderOperations:
    """Build payload operations from module-level symbols (patch-friendly for tests)."""
    return build_payload_builder_operations_with_overrides(
        {
            "build_default_run_summary": build_default_run_summary,
            "build_shayan_panel": build_shayan_panel,
            "build_maintenance_panel": build_maintenance_panel,
            "build_library_panel": build_library_panel,
            "get_library_dataset_stats": get_library_dataset_stats,
            "build_database_state_snapshot": build_database_state_snapshot,
            "get_classification_detail": get_classification_detail,
            "get_personality_overview": get_personality_overview,
            "get_publisher_overview": get_publisher_overview,
            "get_collection_overview": get_collection_overview,
            "get_normalization_dashboard": get_normalization_dashboard,
            "get_normalization_quality": get_normalization_quality,
            "list_suggestions": list_suggestions,
            "list_normalization_history": list_normalization_history,
        }
    )


def _normalization_operations() -> NormalizationOperations:
    """Build normalization operations from module-level symbols."""
    return build_normalization_operations_with_overrides(
        {
            "get_review_queue": get_review_queue,
            "list_canonicals": list_canonicals,
            "create_canonical": create_canonical,
            "link_alias": link_alias,
            "create_and_link_alias": create_and_link_alias,
            "reject_alias": reject_alias,
            "bulk_link_aliases": bulk_link_aliases,
            "bulk_reject_aliases": bulk_reject_aliases,
            "list_suggestions": list_suggestions,
            "refresh_suggestions": refresh_suggestions,
            "get_normalization_merge_candidates": get_normalization_merge_candidates,
            "merge_canonicals": merge_canonicals,
            "list_normalization_history": list_normalization_history,
            "undo_event": undo_event,
            "get_normalization_quality": get_normalization_quality,
            "get_normalization_evidence": get_normalization_evidence,
        }
    )


def _classification_operations() -> ClassificationOperations:
    """Build classification operations from module-level symbols."""
    return build_classification_operations_with_overrides(
        {
            "list_classifications": list_classifications,
            "get_classification_insights": get_classification_insights,
            "get_normalization_preview": get_normalization_preview,
            "get_merge_candidates": get_merge_candidates,
            "merge_classifications": merge_classifications,
        }
    )


def _entities_operations() -> EntitiesOperations:
    """Build entities operations from module-level symbols."""
    return build_entities_operations_with_overrides(
        {
            "list_personalities": list_personalities,
            "get_personality_insights": get_personality_insights,
            "list_publishers": list_publishers,
            "get_publisher_insights": get_publisher_insights,
            "list_library_collections": list_library_collections,
            "get_collection_insights": get_collection_insights,
            "get_collection_review": get_collection_review,
            "list_collection_items": list_collection_items,
            "update_collection": update_collection,
        }
    )


class AppState:
    """Typed state holder for shared app services."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.database_url, schema=settings.database_schema)
        self.runner = TaskRunner(self.db)
        self.shutting_down = False
        self.workflow_service = WorkflowService(
            self.db,
            self.runner,
        )


settings = load_settings()
state = AppState(settings)
payload_builder = PayloadBuilder(
    state_provider=lambda: state,
    panel_defs_provider=lambda: _PANEL_DEFS,
    normalization_entity_types=NORMALIZATION_ENTITY_TYPES,
    slug_separator_pattern=_SLUG_SEPARATOR_PATTERN,
    slug_clean_pattern=_SLUG_CLEAN_PATTERN,
    ops_provider=_payload_builder_operations,
)


def _build_startup_registry() -> Dict[str, list[Dict[str, Any]]]:
    registry = build_startup_seed_registry(
        state.settings,
        panel_defs=_PANEL_DEFS,
        shayan_task_definitions=shayan_task_definitions,
        maintenance_task_definitions=maintenance_task_definitions,
        library_task_definitions=library_task_definitions,
        shayan_workflow_bundle=shayan_workflow_bundle,
        maintenance_backup_full_workflow_bundle=maintenance_backup_full_workflow_bundle,
        maintenance_backup_incr_workflow_bundle=maintenance_backup_incr_workflow_bundle,
        library_workflow_bundle=library_workflow_bundle,
        library_personality_normalization_workflow_bundle=library_personality_normalization_workflow_bundle,
        library_publisher_normalization_workflow_bundle=library_publisher_normalization_workflow_bundle,
    )
    return {
        "panel_defs": registry.panel_defs,
        "task_defs": registry.task_defs,
        "workflow_bundles": registry.workflow_bundles,
    }


def _startup() -> None:
    """Initialize schema and seed known task/workflow definitions."""
    registry = _build_startup_registry()
    startup_app(
        state=state,
        panel_defs=registry["panel_defs"],
        task_defs=registry["task_defs"],
        workflow_bundles=registry["workflow_bundles"],
    )


def _shutdown() -> None:
    """Stop background scheduler worker on app shutdown."""
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
    normalization_operations_provider=_normalization_operations,
    classification_operations_provider=_classification_operations,
    entities_operations_provider=_entities_operations,
)
app = _factory_result.app
run_logs = _factory_result.stream_handlers["run_logs"]
events_stream = _factory_result.stream_handlers["events_stream"]


@app.get("/api/health")
def health() -> Dict[str, str]:
    """Simple health probe endpoint."""
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}
