"""Task registrations owned by the Library flow."""

from __future__ import annotations
from typing import Any

LIBRARY_GENERATE_BOOK_PREVIEWS_TASK_ID = "library.generate_book_previews"
LIBRARY_PREPARE_DOCUMENT_CLEANUP_TASK_ID = "library.prepare_document_cleanup"
LIBRARY_METADATA_EXTRACT_TASK_ID = "library.metadata_extract"
LIBRARY_EXTRACT_NON_PDF_TASK_ID = "library.extract_non_pdf"
LIBRARY_SITE_EXPORT_TASK_ID = "library.site_export"
LIBRARY_NORMALIZE_PERSONALITIES_TASK_ID = "library.normalize_personalities"


def library_task_definitions() -> list[dict[str, Any]]:
    """Return registrations for Python task handlers."""
    return [
        {
            "task_id": "library.suggest_publisher_merges",
            "group_id": "library",
            "title": "Cluster publishers",
            "requires_full_inventory": True,
        },
        {
            "task_id": LIBRARY_NORMALIZE_PERSONALITIES_TASK_ID,
            "group_id": "library",
            "title": "Normalize personalities",
        },
        {
            "task_id": LIBRARY_SITE_EXPORT_TASK_ID,
            "group_id": "library",
            "title": "Export static library",
            "requires_full_inventory": True,
        },
        {
            "task_id": LIBRARY_EXTRACT_NON_PDF_TASK_ID,
            "group_id": "library",
            "title": "Extract non-PDF",
        },
        {
            "task_id": LIBRARY_METADATA_EXTRACT_TASK_ID,
            "group_id": "metadata",
            "title": "Extract metadata",
        },
        {
            "task_id": LIBRARY_PREPARE_DOCUMENT_CLEANUP_TASK_ID,
            "group_id": "maintenance",
            "title": "Cleanup plan",
        },
        {
            "task_id": LIBRARY_GENERATE_BOOK_PREVIEWS_TASK_ID,
            "group_id": "library",
            "title": "Generate book previews",
        },
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
