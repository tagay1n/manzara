"""Task registrations owned by the Collections flow."""

from __future__ import annotations
from typing import Any

from app.modules.library.collection_constants import (
    COLLECTIONS_PANEL_ID,
    COLLECTION_DETECT_TASK_ID,
)


def collection_task_definitions() -> list[dict[str, Any]]:
    """Return registrations for Python task handlers."""
    return [
        {
            "task_id": COLLECTION_DETECT_TASK_ID,
            "group_id": COLLECTIONS_PANEL_ID,
            "title": "Discover collections",
            "requires_full_inventory": True,
        },
    ]


__all__ = [
    "collection_task_definitions",
]
