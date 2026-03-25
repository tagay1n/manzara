"""Manzara MVP API and dashboard UI server."""

from __future__ import annotations

from contextlib import asynccontextmanager
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import Database
from app.control_routes import register_control_routes
from app.core_read_routes import register_core_read_routes
from app.library_classification_routes import register_library_classification_routes
from app.library_entities_routes import register_library_entities_routes
from app.library_normalization_routes import register_library_normalization_routes
from app.modules.library.insights import (
    get_classification_detail,
    get_classification_insights,
    get_merge_candidates,
    get_normalization_preview,
    list_classifications,
)
from app.modules.library.collections import (
    get_collection_insights,
    get_collection_overview,
    list_collection_items,
    list_collections as list_library_collections,
    update_collection,
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
from app.modules.library.stats import get_library_dataset_stats
from app.modules.maintenance.panel import (
    build_database_state_snapshot,
    build_library_panel,
    build_maintenance_panel,
)
from app.modules.maintenance.tasks import (
    MONOCORPUS_META_EVALUATE_TASK_ID,
    maintenance_task_definitions,
)
from app.modules.maintenance.workflow import (
    maintenance_backup_full_workflow_bundle,
    maintenance_backup_incr_workflow_bundle,
    library_personality_normalization_workflow_bundle,
    library_publisher_normalization_workflow_bundle,
    library_workflow_bundle,
)
from app.modules.shayan.panel import build_shayan_panel
from app.modules.shayan.tasks import shayan_task_definitions
from app.modules.shayan.workflow import (
    shayan_workflow_bundle,
)
from app.modules.oscar.panel import build_oscar_panel
from app.modules.oscar.tasks import oscar_task_definitions
from app.modules.oscar.workflow import oscar_pipeline_workflow_bundle
from app.page_routes import register_page_routes
from app.payload_builder import PayloadBuilder
from app.stream_routes import register_stream_routes
from app.settings import Settings, load_settings
from app.run_summary import build_default_run_summary
from app.tasks import TaskRunner
from app.workflows import WorkflowService

APP_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_ROOT / "static"
_SSE_POLL_INTERVAL_SECONDS = 1.0
_SSE_HEARTBEAT_EVERY_EMPTY_POLLS = 15
_TITLE_MAX_LENGTH = 80
_SLUG_SEPARATOR_PATTERN = re.compile(r"[\s_]+")
_SLUG_CLEAN_PATTERN = re.compile(r"[^\w-]+", flags=re.UNICODE)
_PANEL_DEFS = [
    {"panel_id": "shayan", "title": "Shayan"},
    {"panel_id": "maintenance", "title": "Maintenance"},
    {"panel_id": "oscar", "title": "Oscar"},
    {"panel_id": "library", "title": "Library"},
]


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
            shayan_snapshot_file=settings.shayan.latest_snapshot_file,
        )


settings = load_settings()
state = AppState(settings)
payload_builder = PayloadBuilder(
    state_provider=lambda: state,
    panel_defs_provider=lambda: _PANEL_DEFS,
    normalization_entity_types=NORMALIZATION_ENTITY_TYPES,
    slug_separator_pattern=_SLUG_SEPARATOR_PATTERN,
    slug_clean_pattern=_SLUG_CLEAN_PATTERN,
    ops_provider=lambda: {
        "build_default_run_summary": build_default_run_summary,
        "build_shayan_panel": build_shayan_panel,
        "build_maintenance_panel": build_maintenance_panel,
        "build_library_panel": build_library_panel,
        "build_oscar_panel": build_oscar_panel,
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
        "monocorpus_meta_evaluate_task_id": MONOCORPUS_META_EVALUATE_TASK_ID,
    },
)


def _startup() -> None:
    """Initialize schema and seed known task/workflow definitions."""
    state.shutting_down = False
    state.db.init_schema()
    state.db.seed_panels(_PANEL_DEFS)
    task_defs = [
        *shayan_task_definitions(state.settings.shayan),
        *maintenance_task_definitions(state.settings.maintenance),
        *oscar_task_definitions(state.settings.oscar),
    ]
    state.db.seed_tasks(task_defs)
    state.db.seed_workflow_bundle(shayan_workflow_bundle(state.settings.shayan))
    state.db.seed_workflow_bundle(maintenance_backup_full_workflow_bundle())
    state.db.seed_workflow_bundle(maintenance_backup_incr_workflow_bundle())
    state.db.seed_workflow_bundle(library_workflow_bundle())
    state.db.seed_workflow_bundle(library_personality_normalization_workflow_bundle())
    state.db.seed_workflow_bundle(library_publisher_normalization_workflow_bundle())
    state.db.seed_workflow_bundle(oscar_pipeline_workflow_bundle())

    recovered_runs = state.db.recover_active_runs()
    if recovered_runs > 0:
        state.db.insert_event(
            "system.recovery",
            task_id=None,
            run_id=None,
            panel_id=None,
            payload={"recovered_runs": recovered_runs},
        )

    recovered_workflows = state.db.recover_active_workflow_runs()
    if recovered_workflows > 0:
        state.db.insert_event(
            "system.workflow_recovery",
            task_id=None,
            run_id=None,
            panel_id=None,
            payload={"recovered_workflow_runs": recovered_workflows},
        )

    if state.settings.scheduler_enabled:
        state.workflow_service.start()


def _shutdown() -> None:
    """Stop background scheduler worker on app shutdown."""
    state.shutting_down = True
    state.workflow_service.stop()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """FastAPI lifespan hook for startup/shutdown orchestration."""
    _startup()
    try:
        yield
    finally:
        _shutdown()


app = FastAPI(title="Manzara", version="0.1.0", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
register_page_routes(
    app,
    static_dir=STATIC_DIR,
    normalization_entity_types=NORMALIZATION_ENTITY_TYPES,
)
register_control_routes(
    app,
    state_provider=lambda: state,
    title_max_length=_TITLE_MAX_LENGTH,
)
_stream_route_handlers = register_stream_routes(
    app,
    state_provider=lambda: state,
    sse_poll_interval_seconds=_SSE_POLL_INTERVAL_SECONDS,
    sse_heartbeat_every_empty_polls=_SSE_HEARTBEAT_EVERY_EMPTY_POLLS,
)
run_logs = _stream_route_handlers["run_logs"]
events_stream = _stream_route_handlers["events_stream"]


@app.get("/api/health")
def health() -> Dict[str, str]:
    """Simple health probe endpoint."""
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

def build_dashboard_payload() -> Dict[str, Any]:
    return payload_builder.build_dashboard_payload()


def build_schedules_payload() -> Dict[str, Any]:
    return payload_builder.build_schedules_payload()


def build_tasks_payload() -> Dict[str, Any]:
    return payload_builder.build_tasks_payload()


def build_task_detail_payload(task_key: str, limit: int = 20) -> Dict[str, Any]:
    return payload_builder.build_task_detail_payload(task_key, limit=limit)


def build_flow_detail_payload(flow_key: str, limit_per_task: int = 20) -> Dict[str, Any]:
    return payload_builder.build_flow_detail_payload(flow_key, limit_per_task=limit_per_task)


def build_library_payload() -> Dict[str, Any]:
    return payload_builder.build_library_payload()


def build_database_state_payload() -> Dict[str, Any]:
    return payload_builder.build_database_state_payload()


def build_classification_detail_payload(
    classification_id: int,
    *,
    docs_page: int = 1,
    docs_page_size: int = 40,
) -> Dict[str, Any]:
    return payload_builder.build_classification_detail_payload(
        classification_id,
        docs_page=docs_page,
        docs_page_size=docs_page_size,
    )


def build_personality_payload() -> Dict[str, Any]:
    return payload_builder.build_personality_payload()


def build_publisher_payload() -> Dict[str, Any]:
    return payload_builder.build_publisher_payload()


def build_normalization_payload(entity_type: str) -> Dict[str, Any]:
    return payload_builder.build_normalization_payload(entity_type)


def build_collections_payload() -> Dict[str, Any]:
    return payload_builder.build_collections_payload()


register_library_normalization_routes(
    app,
    state_provider=lambda: state,
    normalization_entity_types=NORMALIZATION_ENTITY_TYPES,
    build_normalization_payload=build_normalization_payload,
    operations_provider=lambda: {
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
    },
)
register_library_classification_routes(
    app,
    operations_provider=lambda: {
        "list_classifications": list_classifications,
        "get_classification_insights": get_classification_insights,
        "get_normalization_preview": get_normalization_preview,
        "get_merge_candidates": get_merge_candidates,
    },
    build_classification_detail_payload=build_classification_detail_payload,
)
register_library_entities_routes(
    app,
    state_provider=lambda: state,
    operations_provider=lambda: {
        "list_personalities": list_personalities,
        "get_personality_insights": get_personality_insights,
        "list_publishers": list_publishers,
        "get_publisher_insights": get_publisher_insights,
        "list_library_collections": list_library_collections,
        "get_collection_insights": get_collection_insights,
        "list_collection_items": list_collection_items,
        "update_collection": update_collection,
    },
    build_personality_payload=build_personality_payload,
    build_publisher_payload=build_publisher_payload,
    build_collections_payload=build_collections_payload,
)
register_core_read_routes(
    app,
    payload_provider=lambda: {
        "build_dashboard_payload": build_dashboard_payload,
        "build_schedules_payload": build_schedules_payload,
        "build_tasks_payload": build_tasks_payload,
        "build_task_detail_payload": build_task_detail_payload,
        "build_flow_detail_payload": build_flow_detail_payload,
        "build_library_payload": build_library_payload,
        "build_database_state_payload": build_database_state_payload,
    },
)
