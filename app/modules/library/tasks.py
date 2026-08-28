"""Task definitions owned by the Library flow."""

from __future__ import annotations

from pathlib import Path
from typing import Any


LIBRARY_GENERATE_BOOK_PREVIEWS_TASK_ID = "library.generate_book_previews"
LIBRARY_PREPARE_DOCUMENT_CLEANUP_TASK_ID = "library.prepare_document_cleanup"
LIBRARY_METADATA_EXTRACT_TASK_ID = "library.metadata_extract"
LIBRARY_METADATA_VALIDATE_TASK_ID = "library.metadata_validate"
LIBRARY_EXTRACT_NON_PDF_TASK_ID = "library.extract_non_pdf"


def library_task_definitions(*, app_root: Path | None = None) -> list[dict[str, Any]]:
    """Return task definitions implemented by the Library module."""
    root = app_root or Path(__file__).resolve().parents[3]
    runner = root / "app" / "modules" / "library" / "runtime" / "run_generate_book_previews.py"
    py_bootstrap = 'PY_BIN=".venv/bin/python"; [ -x "$PY_BIN" ] || PY_BIN="python3"; '
    return [
        {
            "task_id": LIBRARY_METADATA_VALIDATE_TASK_ID,
            "panel_id": "library",
            "title": "Validate metadata",
            "task_type": "scan",
            "icon_idle": "ListChecks",
            "icon_running": "Square",
            "cwd": str(root),
            "command": {
                "mode": "shell",
                "value": py_bootstrap
                + '"$PY_BIN" -m app.modules.library.runtime.run_metadata_validate',
            },
        },
        {
            "task_id": LIBRARY_EXTRACT_NON_PDF_TASK_ID,
            "panel_id": "library",
            "title": "Extract non-pdf",
            "task_type": "extract",
            "icon_idle": "FileText",
            "icon_running": "Square",
            "cwd": str(root),
            "command": {
                "mode": "shell",
                "value": py_bootstrap
                + '"$PY_BIN" -m app.modules.library.runtime.run_extract_non_pdf',
            },
        },
        {
            "task_id": LIBRARY_METADATA_EXTRACT_TASK_ID,
            "gemini_workers_default": 1,
            "panel_id": "library",
            "title": "Extract metadata",
            "task_type": "metadata",
            "icon_idle": "ScanText",
            "icon_running": "Square",
            "cwd": str(root),
            "command": {
                "mode": "shell",
                "value": py_bootstrap
                + '"$PY_BIN" -m app.modules.library.runtime.run_metadata_extract',
            },
        },
        {
            "task_id": LIBRARY_PREPARE_DOCUMENT_CLEANUP_TASK_ID,
            "panel_id": "maintenance",
            "title": "Cleanup plan",
            "task_type": "scan",
            "icon_idle": "ListFilter",
            "icon_running": "Square",
            "cwd": str(root),
            "command": {
                "mode": "shell",
                "value": py_bootstrap
                + '"$PY_BIN" -m app.modules.library.runtime.run_prepare_document_cleanup',
            },
        },
        {
            "task_id": LIBRARY_GENERATE_BOOK_PREVIEWS_TASK_ID,
            "panel_id": "library",
            "title": "Generate book previews",
            "task_type": "preview",
            "icon_idle": "Images",
            "icon_running": "Square",
            "cwd": str(root),
            "command": {
                "mode": "shell",
                "value": py_bootstrap + f'"$PY_BIN" "{runner}"',
            },
        },
    ]


__all__ = [
    "LIBRARY_GENERATE_BOOK_PREVIEWS_TASK_ID",
    "LIBRARY_PREPARE_DOCUMENT_CLEANUP_TASK_ID",
    "LIBRARY_METADATA_EXTRACT_TASK_ID",
    "LIBRARY_METADATA_VALIDATE_TASK_ID",
    "LIBRARY_EXTRACT_NON_PDF_TASK_ID",
    "library_task_definitions",
]
