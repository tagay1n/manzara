"""Task registrations for the Maintenance module."""

from __future__ import annotations
from typing import Any

MONOCORPUS_META_EVALUATE_TASK_ID = "maintenance.monocorpus_meta_evaluate"
MAINTENANCE_DOCUMENT_S3_SYNC_TASK_ID = "maintenance.sync_documents_s3"
MAINTENANCE_MONOCORPUS_SYNC_TASK_ID = "maintenance.monocorpus_sync"


def maintenance_task_definitions() -> list[dict[str, Any]]:
    """Return registrations for Python task handlers."""
    return [
        {
            "task_id": MAINTENANCE_MONOCORPUS_SYNC_TASK_ID,
            "group_id": "maintenance",
            "title": "Sync",
        },
        {
            "task_id": MAINTENANCE_DOCUMENT_S3_SYNC_TASK_ID,
            "group_id": "maintenance",
            "title": "Upload to Backblaze S3",
        },
        {
            "task_id": MONOCORPUS_META_EVALUATE_TASK_ID,
            "workers_default": 1,
            "group_id": "metadata",
            "title": "Evaluate metadata",
        },
    ]
