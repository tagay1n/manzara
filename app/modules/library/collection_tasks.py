"""Task registrations owned by the Collections flow."""

from __future__ import annotations
from typing import Any

from app.modules.library.collection_constants import (
    COLLECTIONS_PANEL_ID,
    COLLECTION_APPLY_TASK_ID,
    COLLECTION_DETECT_TASK_ID,
    COLLECTION_VALIDATE_TASK_ID,
)


def collection_task_definitions() -> list[dict[str, Any]]:
    """Return registrations for Python task handlers."""
    return [
        {
            "task_id": COLLECTION_DETECT_TASK_ID,
            "group_id": COLLECTIONS_PANEL_ID,
            "title": "Discover collections",
        },
        {
            "task_id": COLLECTION_VALIDATE_TASK_ID,
            "workers_default": 1,
            "group_id": COLLECTIONS_PANEL_ID,
            "title": "Validate collection proposals",
        },
        {
            "task_id": COLLECTION_APPLY_TASK_ID,
            "group_id": COLLECTIONS_PANEL_ID,
            "title": "Apply collection overrides",
        },
    ]


__all__ = [
    "collection_task_definitions",
]
