"""Task registrations owned by the Library flow."""

from __future__ import annotations

from app.task_runtime.contracts import TaskRegistration

LIBRARY_GENERATE_BOOK_PREVIEWS_TASK_ID = "library.generate_book_previews"
LIBRARY_PREPARE_DOCUMENT_CLEANUP_TASK_ID = "library.prepare_document_cleanup"
LIBRARY_METADATA_EXTRACT_TASK_ID = "library.metadata_extract"
LIBRARY_EXTRACT_NON_PDF_TASK_ID = "library.extract_non_pdf"
LIBRARY_SITE_EXPORT_TASK_ID = "library.site_export"
LIBRARY_NORMALIZE_PERSONALITIES_TASK_ID = "library.normalize_personalities"


def library_task_definitions() -> list[TaskRegistration]:
    """Return registrations for Python task handlers."""
    return [
        TaskRegistration(
            task_id="library.suggest_publisher_merges", title="Cluster publishers",
            group="Library", group_id="library",
            handler_module="app.modules.library.runtime.run_suggest_publisher_merges",
            requires_full_inventory=True,
        ),
        TaskRegistration(
            task_id=LIBRARY_NORMALIZE_PERSONALITIES_TASK_ID, title="Normalize personalities",
            group="Library", group_id="library",
            handler_module="app.modules.library.runtime.run_normalize_personalities",
        ),
        TaskRegistration(
            task_id=LIBRARY_SITE_EXPORT_TASK_ID, title="Export static library",
            group="Library", group_id="library",
            handler_module="app.modules.library.runtime.run_site_export",
            requires_full_inventory=True,
        ),
        TaskRegistration(
            task_id=LIBRARY_EXTRACT_NON_PDF_TASK_ID, title="Extract non-PDF",
            group="Library", group_id="library",
            handler_module="app.modules.library.runtime.run_extract_non_pdf",
        ),
        TaskRegistration(
            task_id=LIBRARY_METADATA_EXTRACT_TASK_ID, title="Extract metadata",
            group="Library", group_id="metadata",
            handler_module="app.modules.library.runtime.run_metadata_extract",
        ),
        TaskRegistration(
            task_id=LIBRARY_PREPARE_DOCUMENT_CLEANUP_TASK_ID, title="Cleanup plan",
            group="Maintenance", group_id="maintenance",
            handler_module="app.modules.library.runtime.run_prepare_document_cleanup",
            interactive=False,
        ),
        TaskRegistration(
            task_id=LIBRARY_GENERATE_BOOK_PREVIEWS_TASK_ID, title="Generate book previews",
            group="Library", group_id="library",
            handler_module="app.modules.library.runtime.run_generate_book_previews",
        ),
    ]


__all__ = [
    "LIBRARY_GENERATE_BOOK_PREVIEWS_TASK_ID",
    "LIBRARY_PREPARE_DOCUMENT_CLEANUP_TASK_ID",
    "LIBRARY_METADATA_EXTRACT_TASK_ID",
    "LIBRARY_EXTRACT_NON_PDF_TASK_ID",
    "LIBRARY_SITE_EXPORT_TASK_ID",
    "LIBRARY_NORMALIZE_PERSONALITIES_TASK_ID",
    "library_task_definitions",
]
