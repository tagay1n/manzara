"""Shared typing contracts for application wiring and providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Protocol, TypedDict

JSONDict = Dict[str, Any]


class PayloadBuilderOperations(Protocol):
    build_default_run_summary: Callable[[JSONDict], JSONDict]
    build_shayan_panel: Callable[..., JSONDict]
    build_maintenance_panel: Callable[..., JSONDict]
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


class RoutePayloadBuilders(Protocol):
    build_system_state_payload: Callable[[], JSONDict]
    build_dashboard_payload: Callable[[], JSONDict]
    build_schedules_payload: Callable[[], JSONDict]
    build_tasks_payload: Callable[[], JSONDict]
    build_task_detail_payload: Callable[..., JSONDict]
    build_flow_detail_payload: Callable[..., JSONDict]
    build_library_payload: Callable[[], JSONDict]
    build_database_state_payload: Callable[[], JSONDict]
    build_classification_detail_payload: Callable[..., JSONDict]
    build_personality_payload: Callable[[], JSONDict]
    build_publisher_payload: Callable[[], JSONDict]
    build_normalization_payload: Callable[[str], JSONDict]
    build_collections_payload: Callable[[], JSONDict]


class CoreReadPayloadBuilders(Protocol):
    build_system_state_payload: Callable[[], JSONDict]
    build_dashboard_payload: Callable[[], JSONDict]
    build_schedules_payload: Callable[[], JSONDict]
    build_tasks_payload: Callable[[], JSONDict]
    build_task_detail_payload: Callable[..., JSONDict]
    build_flow_detail_payload: Callable[..., JSONDict]
    build_library_payload: Callable[[], JSONDict]
    build_database_state_payload: Callable[[], JSONDict]


class NormalizationOperations(Protocol):
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


class ClassificationOperations(Protocol):
    list_classifications: Callable[..., Any]
    get_classification_insights: Callable[..., Any]
    get_normalization_preview: Callable[..., Any]
    get_merge_candidates: Callable[..., Any]
    merge_classifications: Callable[..., Any]


class EntitiesOperations(Protocol):
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


class StreamRouteHandlers(TypedDict):
    run_logs: Callable[..., Any]
    events_stream: Callable[..., Any]


class StateProvider(Protocol):
    def __call__(self) -> Any: ...


class RoutePayloadBuildersProvider(Protocol):
    def __call__(self) -> RoutePayloadBuilders: ...


class NormalizationOperationsProvider(Protocol):
    def __call__(self) -> NormalizationOperations: ...


class ClassificationOperationsProvider(Protocol):
    def __call__(self) -> ClassificationOperations: ...


class EntitiesOperationsProvider(Protocol):
    def __call__(self) -> EntitiesOperations: ...


@dataclass(frozen=True)
class StartupSeedRegistry:
    """Startup seed bundles for panels/tasks/workflows."""

    panel_defs: list[JSONDict]
    task_defs: list[JSONDict]
    workflow_bundles: list[JSONDict]
