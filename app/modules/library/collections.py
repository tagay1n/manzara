"""Public collection operations.

Collection discovery and review proposals are intentionally separate from the
canonical collection catalog so detector reruns cannot mutate approved data.
"""

from app.modules.library.collection_catalog import (
    apply_collection_overrides,
    decide_collection_proposal,
    get_collection_insights,
    get_collection_overview,
    get_collection_proposal_review,
    get_collection_review,
    list_collection_items,
    list_collection_proposals,
    list_collections,
    merge_collections,
    update_collection,
)
from app.modules.library.collection_detection import (
    CollectionEligibilityPolicy,
    build_document_features,
    discover_collections,
    normalize_collection_text,
    title_core,
)

# Existing runtime/import name retained as the public operation name, not as a
# compatibility implementation.
detect_collections = discover_collections

__all__ = [
    "CollectionEligibilityPolicy",
    "apply_collection_overrides",
    "build_document_features",
    "decide_collection_proposal",
    "detect_collections",
    "discover_collections",
    "get_collection_insights",
    "get_collection_overview",
    "get_collection_proposal_review",
    "get_collection_review",
    "list_collection_items",
    "list_collection_proposals",
    "list_collections",
    "merge_collections",
    "normalize_collection_text",
    "title_core",
    "update_collection",
]
