"""Dependency and operation-service builders for app wiring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.contracts import (
    ClassificationOperations,
    CoreReadPayloadBuilders,
    EntitiesOperations,
    JSONDict,
    NormalizationOperations,
    PayloadBuilderOperations,
    RoutePayloadBuilders,
)
from app.modules.library.collections import (
    decide_collection_proposal,
    get_collection_insights,
    get_collection_overview,
    get_collection_proposal_review,
    get_collection_review,
    list_collection_items,
    list_collection_proposals,
    list_collections as list_library_collections,
    merge_collections,
    update_collection,
)
from app.modules.library.classification_insights import (
    get_classification_insights,
    list_classifications,
)
from app.modules.library.classification_operations import (
    get_classification_detail,
    get_merge_candidates,
    get_normalization_preview,
    merge_classifications,
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
    merge_canonicals,
    reject_alias,
    undo_event,
)
from app.modules.library.normalization_suggestions import (
    list_suggestions,
    refresh_suggestions,
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
    build_backup_panel,
    build_database_state_snapshot,
    build_library_panel,
    build_maintenance_panel,
)
from app.modules.maintenance.tasks import MONOCORPUS_META_EVALUATE_TASK_ID
from app.payload_builder import PayloadBuilder
from app.run_summary import build_default_run_summary


@dataclass(frozen=True)
class PayloadBuilderOperationsService:
    build_default_run_summary: Callable[[JSONDict], JSONDict]
    build_maintenance_panel: Callable[..., JSONDict]
    build_backup_panel: Callable[..., JSONDict]
    build_library_panel: Callable[..., JSONDict]
    get_library_dataset_stats: Callable[..., JSONDict]
    build_database_state_snapshot: Callable[..., JSONDict]
    get_classification_detail: Callable[..., JSONDict]
    get_personality_overview: Callable[..., JSONDict]
    get_publisher_overview: Callable[..., JSONDict]
    get_collection_overview: Callable[..., JSONDict]
    get_normalization_dashboard: Callable[..., JSONDict]
    get_normalization_quality: Callable[..., JSONDict]
    list_suggestions: Callable[..., list[JSONDict]]
    list_normalization_history: Callable[..., list[JSONDict]]
    monocorpus_meta_evaluate_task_id: str


@dataclass(frozen=True)
class RoutePayloadBuildersService:
    build_system_state_payload: Callable[[], JSONDict]
    build_dashboard_payload: Callable[[], JSONDict]
    build_tasks_payload: Callable[[], JSONDict]
    build_task_detail_payload: Callable[..., JSONDict]
    build_library_payload: Callable[[], JSONDict]
    build_database_state_payload: Callable[[], JSONDict]
    build_classification_detail_payload: Callable[..., JSONDict]
    build_personality_payload: Callable[[], JSONDict]
    build_publisher_payload: Callable[[], JSONDict]
    build_normalization_payload: Callable[[str], JSONDict]
    build_collections_payload: Callable[[], JSONDict]


@dataclass(frozen=True)
class CoreReadPayloadBuildersService:
    build_system_state_payload: Callable[[], JSONDict]
    build_dashboard_payload: Callable[[], JSONDict]
    build_tasks_payload: Callable[[], JSONDict]
    build_task_detail_payload: Callable[..., JSONDict]
    build_library_payload: Callable[[], JSONDict]
    build_database_state_payload: Callable[[], JSONDict]


@dataclass(frozen=True)
class NormalizationOperationsService:
    get_review_queue: Callable[..., Any]
    list_canonicals: Callable[..., Any]
    create_canonical: Callable[..., Any]
    link_alias: Callable[..., Any]
    create_and_link_alias: Callable[..., Any]
    reject_alias: Callable[..., Any]
    bulk_link_aliases: Callable[..., Any]
    bulk_reject_aliases: Callable[..., Any]
    list_suggestions: Callable[..., Any]
    refresh_suggestions: Callable[..., Any]
    get_normalization_merge_candidates: Callable[..., Any]
    merge_canonicals: Callable[..., Any]
    list_normalization_history: Callable[..., Any]
    undo_event: Callable[..., Any]
    get_normalization_quality: Callable[..., Any]
    get_normalization_evidence: Callable[..., Any]


@dataclass(frozen=True)
class ClassificationOperationsService:
    list_classifications: Callable[..., Any]
    get_classification_insights: Callable[..., Any]
    get_normalization_preview: Callable[..., Any]
    get_merge_candidates: Callable[..., Any]
    merge_classifications: Callable[..., Any]


@dataclass(frozen=True)
class EntitiesOperationsService:
    list_personalities: Callable[..., Any]
    get_personality_insights: Callable[..., Any]
    list_publishers: Callable[..., Any]
    get_publisher_insights: Callable[..., Any]
    list_library_collections: Callable[..., Any]
    get_collection_insights: Callable[..., Any]
    get_collection_review: Callable[..., Any]
    list_collection_items: Callable[..., Any]
    list_collection_proposals: Callable[..., Any]
    get_collection_proposal_review: Callable[..., Any]
    decide_collection_proposal: Callable[..., Any]
    update_collection: Callable[..., Any]
    merge_collections: Callable[..., Any]


def _apply_overrides(service: Any, overrides: dict[str, Any] | None) -> Any:
    if not overrides:
        return service
    return type(service)(**{**service.__dict__, **overrides})


def build_payload_builder_operations() -> PayloadBuilderOperations:
    """Build operation set consumed by PayloadBuilder internals."""
    return PayloadBuilderOperationsService(
        build_default_run_summary=build_default_run_summary,
        build_maintenance_panel=build_maintenance_panel,
        build_backup_panel=build_backup_panel,
        build_library_panel=build_library_panel,
        get_library_dataset_stats=get_library_dataset_stats,
        build_database_state_snapshot=build_database_state_snapshot,
        get_classification_detail=get_classification_detail,
        get_personality_overview=get_personality_overview,
        get_publisher_overview=get_publisher_overview,
        get_collection_overview=get_collection_overview,
        get_normalization_dashboard=get_normalization_dashboard,
        get_normalization_quality=get_normalization_quality,
        list_suggestions=list_suggestions,
        list_normalization_history=list_normalization_history,
        monocorpus_meta_evaluate_task_id=MONOCORPUS_META_EVALUATE_TASK_ID,
    )


def build_payload_builder_operations_with_overrides(
    overrides: dict[str, Any] | None = None,
) -> PayloadBuilderOperations:
    """Build PayloadBuilder operations and apply optional key overrides."""
    return _apply_overrides(build_payload_builder_operations(), overrides)


def build_normalization_operations() -> NormalizationOperations:
    """Build normalization route operations service."""
    return NormalizationOperationsService(
        get_review_queue=get_review_queue,
        list_canonicals=list_canonicals,
        create_canonical=create_canonical,
        link_alias=link_alias,
        create_and_link_alias=create_and_link_alias,
        reject_alias=reject_alias,
        bulk_link_aliases=bulk_link_aliases,
        bulk_reject_aliases=bulk_reject_aliases,
        list_suggestions=list_suggestions,
        refresh_suggestions=refresh_suggestions,
        get_normalization_merge_candidates=get_normalization_merge_candidates,
        merge_canonicals=merge_canonicals,
        list_normalization_history=list_normalization_history,
        undo_event=undo_event,
        get_normalization_quality=get_normalization_quality,
        get_normalization_evidence=get_normalization_evidence,
    )


def build_normalization_operations_with_overrides(
    overrides: dict[str, Any] | None = None,
) -> NormalizationOperations:
    """Build normalization operations and apply optional field overrides."""
    return _apply_overrides(build_normalization_operations(), overrides)


def build_classification_operations() -> ClassificationOperations:
    """Build classification route operations service."""
    return ClassificationOperationsService(
        list_classifications=list_classifications,
        get_classification_insights=get_classification_insights,
        get_normalization_preview=get_normalization_preview,
        get_merge_candidates=get_merge_candidates,
        merge_classifications=merge_classifications,
    )


def build_classification_operations_with_overrides(
    overrides: dict[str, Any] | None = None,
) -> ClassificationOperations:
    """Build classification operations and apply optional field overrides."""
    return _apply_overrides(build_classification_operations(), overrides)


def build_entities_operations() -> EntitiesOperations:
    """Build entities/personality/publisher/collection operations service."""
    return EntitiesOperationsService(
        list_personalities=list_personalities,
        get_personality_insights=get_personality_insights,
        list_publishers=list_publishers,
        get_publisher_insights=get_publisher_insights,
        list_library_collections=list_library_collections,
        get_collection_insights=get_collection_insights,
        get_collection_review=get_collection_review,
        list_collection_items=list_collection_items,
        list_collection_proposals=list_collection_proposals,
        get_collection_proposal_review=get_collection_proposal_review,
        decide_collection_proposal=decide_collection_proposal,
        update_collection=update_collection,
        merge_collections=merge_collections,
    )


def build_entities_operations_with_overrides(
    overrides: dict[str, Any] | None = None,
) -> EntitiesOperations:
    """Build entities operations and apply optional field overrides."""
    return _apply_overrides(build_entities_operations(), overrides)


def build_route_payload_builders(payload_builder: PayloadBuilder) -> RoutePayloadBuilders:
    """Build payload callbacks exposed to API route modules."""
    return RoutePayloadBuildersService(
        build_system_state_payload=payload_builder.build_system_state_payload,
        build_dashboard_payload=payload_builder.build_dashboard_payload,
        build_tasks_payload=payload_builder.build_tasks_payload,
        build_task_detail_payload=payload_builder.build_task_detail_payload,
        build_library_payload=payload_builder.build_library_payload,
        build_database_state_payload=payload_builder.build_database_state_payload,
        build_classification_detail_payload=payload_builder.build_classification_detail_payload,
        build_personality_payload=payload_builder.build_personality_payload,
        build_publisher_payload=payload_builder.build_publisher_payload,
        build_normalization_payload=payload_builder.build_normalization_payload,
        build_collections_payload=payload_builder.build_collections_payload,
    )


def build_core_read_payload_builders(payload_builders: RoutePayloadBuilders) -> CoreReadPayloadBuilders:
    """Select read payload callbacks required by core read routes."""
    return CoreReadPayloadBuildersService(
        build_system_state_payload=payload_builders.build_system_state_payload,
        build_dashboard_payload=payload_builders.build_dashboard_payload,
        build_tasks_payload=payload_builders.build_tasks_payload,
        build_task_detail_payload=payload_builders.build_task_detail_payload,
        build_library_payload=payload_builders.build_library_payload,
        build_database_state_payload=payload_builders.build_database_state_payload,
    )
