"""Task definitions for the Maintenance module."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Dict, List

from app.modules.maintenance.config import MaintenanceSettings


MONOCORPUS_META_EVALUATE_TASK_ID = "maintenance.monocorpus_meta_evaluate"
LIBRARY_PERSONALITY_SUGGESTIONS_REFRESH_TASK_ID = (
    "library.personality_suggestions_refresh"
)
LIBRARY_PUBLISHER_SUGGESTIONS_REFRESH_TASK_ID = "library.publisher_suggestions_refresh"
MAINTENANCE_PGBACKREST_FULL_TASK_ID = "maintenance.pgbackrest_backup_full"
MAINTENANCE_PGBACKREST_INCR_TASK_ID = "maintenance.pgbackrest_backup_incr"
MAINTENANCE_DOCUMENT_S3_SYNC_TASK_ID = "maintenance.sync_documents_s3"
MAINTENANCE_MONOCORPUS_SYNC_TASK_ID = "maintenance.monocorpus_sync"
MAINTENANCE_DUMP_STATE_TASK_ID = "maintenance.dump_state"


def maintenance_task_definitions(settings: MaintenanceSettings) -> List[Dict[str, Any]]:
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
    stanza = shlex.quote(settings.pgbackrest_stanza)
    py_bootstrap = 'PY_BIN=".venv/bin/python"; [ -x "$PY_BIN" ] || PY_BIN="python3"; '
    meta_eval_cmd = py_bootstrap + f'"$PY_BIN" "{meta_eval_runner}"'
    backup_full_cmd = (
        f"sudo -n -u postgres pgbackrest --stanza={stanza} --type=full backup"
    )
    backup_incr_cmd = (
        f"sudo -n -u postgres pgbackrest --stanza={stanza} --type=incr backup"
    )
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
    legacy_credentials_dir = shlex.quote(
        str(settings.monocorpus_repo_path / "_artifacts" / "credentials")
    )
    dump_state_cmd = (
        py_bootstrap
        + '"$PY_BIN" -m app.modules.maintenance.runtime.dump_state '
        + f"--legacy-credentials-dir {legacy_credentials_dir}"
    )

    return [
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
            "task_id": MAINTENANCE_PGBACKREST_FULL_TASK_ID,
            "panel_id": "backup",
            "title": "Full backup",
            "task_type": "backup",
            "icon_idle": "Database",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": backup_full_cmd},
        },
        {
            "task_id": MAINTENANCE_PGBACKREST_INCR_TASK_ID,
            "panel_id": "backup",
            "title": "Incremental backup",
            "task_type": "backup",
            "icon_idle": "Clock3",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": backup_incr_cmd},
        },
        {
            "task_id": MAINTENANCE_DUMP_STATE_TASK_ID,
            "panel_id": "backup",
            "title": "Upload to GSheets",
            "task_type": "backup",
            "icon_idle": "TableProperties",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": dump_state_cmd},
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
