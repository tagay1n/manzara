"""Public normalization workbench API; implementations live in focused owner modules."""

from __future__ import annotations

from app.modules.library.normalization_canonicals import (
    create_canonical,
    create_canonical_group,
    list_canonical_aliases,
    list_canonicals,
    merge_canonicals,
    rename_canonical,
)
from app.modules.library.normalization_decisions import (
    bulk_link_aliases,
    bulk_reject_aliases,
    create_and_link_alias,
    dismiss_suggestion,
    link_alias,
    reject_alias,
)
from app.modules.library.normalization_history import list_history, undo_event
from app.modules.library.normalization_quality import get_merge_candidates, get_quality
from app.modules.library.normalization_queries import get_evidence
from app.modules.library.normalization_rules import ENTITY_TYPES
from app.modules.library.normalization_views import (
    get_normalization_dashboard,
    get_review_queue,
)

__all__ = [
    "ENTITY_TYPES",
    "get_normalization_dashboard",
    "get_review_queue",
    "list_canonicals",
    "create_canonical",
    "create_canonical_group",
    "list_canonical_aliases",
    "rename_canonical",
    "dismiss_suggestion",
    "link_alias",
    "create_and_link_alias",
    "reject_alias",
    "bulk_link_aliases",
    "bulk_reject_aliases",
    "merge_canonicals",
    "undo_event",
    "list_history",
    "get_quality",
    "get_merge_candidates",
    "get_evidence",
]
