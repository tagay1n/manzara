"""Dependency and operation-map builders for app wiring."""

from __future__ import annotations

from typing import Any

from app.contracts import (
    ClassificationOperations,
    CoreReadPayloadBuilders,
    EntitiesOperations,
    PayloadBuilderOperations,
    RoutePayloadBuilders,
    NormalizationOperations,
)
from app.modules.library.collections import (
    get_collection_insights,
    get_collection_overview,
    list_collection_items,
    list_collections as list_library_collections,
    update_collection,
)
from app.modules.library.insights import (
    get_classification_detail,
    get_classification_insights,
    get_merge_candidates,
    get_normalization_preview,
    list_classifications,
)
from app.modules.library.normalization import (
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
from app.modules.maintenance.panel import (
    build_database_state_snapshot,
    build_library_panel,
    build_maintenance_panel,
)
from app.modules.maintenance.tasks import MONOCORPUS_META_EVALUATE_TASK_ID
from app.modules.oscar.panel import build_oscar_panel
from app.modules.shayan.panel import build_shayan_panel
from app.payload_builder import PayloadBuilder
from app.run_summary import build_default_run_summary


def build_payload_builder_operations() -> PayloadBuilderOperations:
    """Build operation set consumed by PayloadBuilder internals."""
    return {
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
    }


def build_payload_builder_operations_with_overrides(
    overrides: dict[str, Any] | None = None,
) -> PayloadBuilderOperations:
    """Build PayloadBuilder operations and apply optional key overrides."""
    operations = build_payload_builder_operations()
    if overrides:
        operations.update(overrides)
    return operations


def build_normalization_operations() -> NormalizationOperations:
    """Build normalization route operations map."""
    return {
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


def build_normalization_operations_with_overrides(
    overrides: dict[str, Any] | None = None,
) -> NormalizationOperations:
    """Build normalization operations and apply optional key overrides."""
    operations = build_normalization_operations()
    if overrides:
        operations.update(overrides)
    return operations


def build_classification_operations() -> ClassificationOperations:
    """Build classification route operations map."""
    return {
        "list_classifications": list_classifications,
        "get_classification_insights": get_classification_insights,
        "get_normalization_preview": get_normalization_preview,
        "get_merge_candidates": get_merge_candidates,
    }


def build_classification_operations_with_overrides(
    overrides: dict[str, Any] | None = None,
) -> ClassificationOperations:
    """Build classification operations and apply optional key overrides."""
    operations = build_classification_operations()
    if overrides:
        operations.update(overrides)
    return operations


def build_entities_operations() -> EntitiesOperations:
    """Build entities/personality/publisher/collection route operations map."""
    return {
        "list_personalities": list_personalities,
        "get_personality_insights": get_personality_insights,
        "list_publishers": list_publishers,
        "get_publisher_insights": get_publisher_insights,
        "list_library_collections": list_library_collections,
        "get_collection_insights": get_collection_insights,
        "list_collection_items": list_collection_items,
        "update_collection": update_collection,
    }


def build_entities_operations_with_overrides(
    overrides: dict[str, Any] | None = None,
) -> EntitiesOperations:
    """Build entities operations and apply optional key overrides."""
    operations = build_entities_operations()
    if overrides:
        operations.update(overrides)
    return operations


def build_route_payload_builders(payload_builder: PayloadBuilder) -> RoutePayloadBuilders:
    """Build payload callbacks exposed to API route modules."""
    return {
        "build_dashboard_payload": payload_builder.build_dashboard_payload,
        "build_schedules_payload": payload_builder.build_schedules_payload,
        "build_tasks_payload": payload_builder.build_tasks_payload,
        "build_task_detail_payload": payload_builder.build_task_detail_payload,
        "build_flow_detail_payload": payload_builder.build_flow_detail_payload,
        "build_library_payload": payload_builder.build_library_payload,
        "build_database_state_payload": payload_builder.build_database_state_payload,
        "build_classification_detail_payload": payload_builder.build_classification_detail_payload,
        "build_personality_payload": payload_builder.build_personality_payload,
        "build_publisher_payload": payload_builder.build_publisher_payload,
        "build_normalization_payload": payload_builder.build_normalization_payload,
        "build_collections_payload": payload_builder.build_collections_payload,
    }


def build_core_read_payload_builders(payload_builders: RoutePayloadBuilders) -> CoreReadPayloadBuilders:
    """Select only read payload callbacks required by core read routes."""
    return {
        "build_dashboard_payload": payload_builders["build_dashboard_payload"],
        "build_schedules_payload": payload_builders["build_schedules_payload"],
        "build_tasks_payload": payload_builders["build_tasks_payload"],
        "build_task_detail_payload": payload_builders["build_task_detail_payload"],
        "build_flow_detail_payload": payload_builders["build_flow_detail_payload"],
        "build_library_payload": payload_builders["build_library_payload"],
        "build_database_state_payload": payload_builders["build_database_state_payload"],
    }
