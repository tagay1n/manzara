"""Task registrations for the Maintenance module."""

from __future__ import annotations

from app.task_runtime.contracts import TaskRegistration

MONOCORPUS_META_EVALUATE_TASK_ID = "maintenance.monocorpus_meta_evaluate"
MAINTENANCE_DOCUMENT_S3_SYNC_TASK_ID = "maintenance.sync_documents_s3"
MAINTENANCE_MONOCORPUS_SYNC_TASK_ID = "maintenance.monocorpus_sync"


def maintenance_task_definitions() -> list[TaskRegistration]:
    """Return registrations for Python task handlers."""
    return [
        TaskRegistration(
            task_id=MAINTENANCE_MONOCORPUS_SYNC_TASK_ID, title="Sync",
            group="Maintenance", group_id="maintenance",
            handler_module="app.modules.maintenance.runtime.sync_monocorpus",
            interactive=False,
        ),
        TaskRegistration(
            task_id=MAINTENANCE_DOCUMENT_S3_SYNC_TASK_ID, title="Upload to Backblaze S3",
            group="Maintenance", group_id="maintenance",
            handler_module="app.modules.maintenance.runtime.sync_documents_s3",
            interactive=False,
        ),
        TaskRegistration(
            task_id=MONOCORPUS_META_EVALUATE_TASK_ID, title="Evaluate metadata",
            group="Maintenance", group_id="metadata",
            handler_module="app.modules.library.runtime.run_meta_evaluate",
        ),
    ]
