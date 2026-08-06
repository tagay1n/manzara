"""Task definitions for the Maintenance module."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Dict, List

from app.modules.maintenance.config import MaintenanceSettings


MONOCORPUS_META_EVALUATE_TASK_ID = "maintenance.monocorpus_meta_evaluate"
LIBRARY_PERSONALITY_SUGGESTIONS_REFRESH_TASK_ID = "library.personality_suggestions_refresh"
LIBRARY_PUBLISHER_SUGGESTIONS_REFRESH_TASK_ID = "library.publisher_suggestions_refresh"
LIBRARY_COLLECTION_DETECT_TASK_ID = "library.collection_detect"
LIBRARY_COLLECTION_VALIDATE_TASK_ID = "library.collection_validate"
LIBRARY_COLLECTION_APPLY_TASK_ID = "library.collection_apply"
MAINTENANCE_PGBACKREST_FULL_TASK_ID = "maintenance.pgbackrest_backup_full"
MAINTENANCE_PGBACKREST_INCR_TASK_ID = "maintenance.pgbackrest_backup_incr"
MAINTENANCE_DOCUMENT_S3_SYNC_TASK_ID = "maintenance.sync_documents_s3"


def maintenance_task_definitions(settings: MaintenanceSettings) -> List[Dict[str, Any]]:
    """Return Maintenance task definitions for dashboard and runtime."""
    app_root = Path(__file__).resolve().parents[3]
    meta_eval_runner = app_root / "app" / "modules" / "library" / "runtime" / "run_meta_evaluate.py"
    norm_refresh_runner = app_root / "app" / "modules" / "library" / "runtime" / "run_normalization_refresh.py"
    collection_detect_runner = (
        app_root / "app" / "modules" / "library" / "runtime" / "run_collection_detect.py"
    )
    collection_validate_runner = (
        app_root / "app" / "modules" / "library" / "runtime" / "run_collection_validate.py"
    )
    collection_apply_runner = (
        app_root / "app" / "modules" / "library" / "runtime" / "run_collection_apply.py"
    )
    stanza = shlex.quote(settings.pgbackrest_stanza)
    py_bootstrap = 'PY_BIN=".venv/bin/python"; [ -x "$PY_BIN" ] || PY_BIN="python3"; '
    meta_eval_cmd = py_bootstrap + f'"$PY_BIN" "{meta_eval_runner}" --workers 1'
    backup_full_cmd = (
        "sudo -n -u postgres "
        f"pgbackrest --stanza={stanza} --type=full backup"
    )
    backup_incr_cmd = (
        "sudo -n -u postgres "
        f"pgbackrest --stanza={stanza} --type=incr backup"
    )
    personality_refresh_cmd = (
        py_bootstrap + f'"$PY_BIN" "{norm_refresh_runner}" --entity-type personality --limit 180'
    )
    publisher_refresh_cmd = (
        py_bootstrap + f'"$PY_BIN" "{norm_refresh_runner}" --entity-type publisher --limit 180'
    )
    collection_detect_cmd = py_bootstrap + f'"$PY_BIN" "{collection_detect_runner}"'
    collection_validate_cmd = py_bootstrap + f'"$PY_BIN" "{collection_validate_runner}"'
    collection_apply_cmd = py_bootstrap + f'"$PY_BIN" "{collection_apply_runner}" --limit 500'
    document_sync_cmd = (
        py_bootstrap + '"$PY_BIN" -m app.modules.maintenance.runtime.sync_documents_s3'
    )

    return [
        {
            "task_id": MAINTENANCE_DOCUMENT_S3_SYNC_TASK_ID,
            "panel_id": "maintenance",
            "title": "Sync documents to S3",
            "task_type": "transfer",
            "icon_idle": "CloudUpload",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": document_sync_cmd},
        },
        {
            "task_id": MAINTENANCE_PGBACKREST_FULL_TASK_ID,
            "panel_id": "maintenance",
            "title": "Postgres full backup",
            "task_type": "backup",
            "icon_idle": "Database",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": backup_full_cmd},
        },
        {
            "task_id": MAINTENANCE_PGBACKREST_INCR_TASK_ID,
            "panel_id": "maintenance",
            "title": "Postgres incremental backup",
            "task_type": "backup",
            "icon_idle": "Clock3",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": backup_incr_cmd},
        },
        {
            "task_id": MONOCORPUS_META_EVALUATE_TASK_ID,
            "panel_id": "library",
            "title": "Monocorpus meta evaluate",
            "task_type": "metadata",
            "icon_idle": "ClipboardCheck",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": meta_eval_cmd},
        },
        {
            "task_id": LIBRARY_COLLECTION_DETECT_TASK_ID,
            "panel_id": "library",
            "title": "Discover collections",
            "task_type": "metadata",
            "icon_idle": "Folders",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": collection_detect_cmd},
        },
        {
            "task_id": LIBRARY_COLLECTION_VALIDATE_TASK_ID,
            "panel_id": "library",
            "title": "Validate collection proposals",
            "task_type": "metadata",
            "icon_idle": "ScanSearch",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": collection_validate_cmd},
        },
        {
            "task_id": LIBRARY_COLLECTION_APPLY_TASK_ID,
            "panel_id": "library",
            "title": "Apply collection overrides",
            "task_type": "metadata",
            "icon_idle": "CheckCheck",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": collection_apply_cmd},
        },
        {
            "task_id": LIBRARY_PERSONALITY_SUGGESTIONS_REFRESH_TASK_ID,
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
            "panel_id": "library",
            "title": "Refresh publisher suggestions",
            "task_type": "metadata",
            "icon_idle": "Sparkles",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": publisher_refresh_cmd},
        },
    ]
