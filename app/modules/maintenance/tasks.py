"""Task definitions for the Maintenance module."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.modules.maintenance.config import MaintenanceSettings

MONOCORPUS_META_EVALUATE_TASK_ID = "maintenance.monocorpus_meta_evaluate"
LIBRARY_PERSONALITY_SUGGESTIONS_REFRESH_TASK_ID = (
    "library.personality_suggestions_refresh"
)
LIBRARY_PUBLISHER_SUGGESTIONS_REFRESH_TASK_ID = "library.publisher_suggestions_refresh"
MAINTENANCE_DOCUMENT_S3_SYNC_TASK_ID = "maintenance.sync_documents_s3"
MAINTENANCE_MONOCORPUS_SYNC_TASK_ID = "maintenance.monocorpus_sync"
MAINTENANCE_MIGRATE_PDF_CONTENT_TASK_ID = "maintenance.migrate_pdf_content"


def maintenance_task_definitions(settings: MaintenanceSettings) -> list[dict[str, Any]]:
    """Return Maintenance task definitions for dashboard and runtime."""
    app_root = Path(__file__).resolve().parents[3]
    meta_eval_runner = (
        app_root / "app" / "modules" / "library" / "runtime" / "run_meta_evaluate.py"
    )
    norm_refresh_runner = (
        app_root
        / "app"
        / "modules"
        / "library"
        / "runtime"
        / "run_normalization_refresh.py"
    )
    py_bootstrap = 'PY_BIN=".venv/bin/python"; [ -x "$PY_BIN" ] || PY_BIN="python3"; '
    meta_eval_cmd = py_bootstrap + f'"$PY_BIN" "{meta_eval_runner}"'
    personality_refresh_cmd = (
        py_bootstrap
        + f'"$PY_BIN" "{norm_refresh_runner}" --entity-type personality --limit 180'
    )
    publisher_refresh_cmd = (
        py_bootstrap
        + f'"$PY_BIN" "{norm_refresh_runner}" --entity-type publisher --limit 180'
    )
    document_sync_cmd = (
        py_bootstrap + '"$PY_BIN" -m app.modules.maintenance.runtime.sync_documents_s3'
    )
    monocorpus_sync_cmd = (
        py_bootstrap + '"$PY_BIN" -m app.modules.maintenance.runtime.sync_monocorpus'
    )
    content_migration_cmd = (
        py_bootstrap
        + '"$PY_BIN" -m app.modules.maintenance.runtime.migrate_pdf_content'
    )

    return [
        {
            "task_id": MAINTENANCE_MIGRATE_PDF_CONTENT_TASK_ID,
            "panel_id": "library",
            "title": "Move PDF content to Backblaze",
            "task_type": "transfer",
            "icon_idle": "CloudUpload",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": content_migration_cmd},
        },
        {
            "task_id": MAINTENANCE_MONOCORPUS_SYNC_TASK_ID,
            "panel_id": "maintenance",
            "title": "Sync",
            "task_type": "sync",
            "icon_idle": "RefreshCw",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": monocorpus_sync_cmd},
        },
        {
            "task_id": MAINTENANCE_DOCUMENT_S3_SYNC_TASK_ID,
            "panel_id": "maintenance",
            "title": "Upload to Backblaze S3",
            "task_type": "transfer",
            "icon_idle": "CloudUpload",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": document_sync_cmd},
        },
        {
            "task_id": MONOCORPUS_META_EVALUATE_TASK_ID,
            "gemini_workers_default": 1,
            "panel_id": "metadata",
            "title": "Evaluate metadata",
            "task_type": "metadata",
            "icon_idle": "ClipboardCheck",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": meta_eval_cmd},
        },
        {
            "task_id": LIBRARY_PERSONALITY_SUGGESTIONS_REFRESH_TASK_ID,
            "gemini_workers_default": 1,
            "panel_id": "library",
            "title": "Refresh personality suggestions",
            "task_type": "metadata",
            "icon_idle": "Sparkles",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": personality_refresh_cmd},
        },
        {
            "task_id": LIBRARY_PUBLISHER_SUGGESTIONS_REFRESH_TASK_ID,
            "gemini_workers_default": 1,
            "panel_id": "library",
            "title": "Refresh publisher suggestions",
            "task_type": "metadata",
            "icon_idle": "Sparkles",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": publisher_refresh_cmd},
        },
    ]
