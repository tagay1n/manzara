"""Task registrations owned by the Collections flow."""

from __future__ import annotations

from app.task_runtime.contracts import TaskRegistration

from app.modules.library.collection_constants import (
    COLLECTIONS_PANEL_ID,
    COLLECTION_DETECT_TASK_ID,
)


def collection_task_definitions() -> list[TaskRegistration]:
    """Return registrations for Python task handlers."""
    return [
        TaskRegistration(
            task_id=COLLECTION_DETECT_TASK_ID, title="Discover collections",
            group="Library", group_id=COLLECTIONS_PANEL_ID,
            handler_module="app.modules.library.runtime.run_collection_detect",
            requires_full_inventory=True,
        ),
    ]


__all__ = [
    "collection_task_definitions",
]
