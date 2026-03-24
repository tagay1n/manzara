"""Task definitions for the Maintenance module."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from app.modules.maintenance.config import MaintenanceSettings


MONOCORPUS_SYNC_TASK_ID = "maintenance.monocorpus_sync"
MONOCORPUS_META_EVALUATE_TASK_ID = "maintenance.monocorpus_meta_evaluate"
LIBRARY_PERSONALITY_SUGGESTIONS_REFRESH_TASK_ID = "library.personality_suggestions_refresh"
LIBRARY_PUBLISHER_SUGGESTIONS_REFRESH_TASK_ID = "library.publisher_suggestions_refresh"


def maintenance_task_definitions(settings: MaintenanceSettings) -> List[Dict[str, Any]]:
    """Return Maintenance task definitions for dashboard and runtime."""
    app_root = Path(__file__).resolve().parents[3]
    sync_runner = app_root / "app" / "modules" / "maintenance" / "runtime" / "run_sync.py"
    meta_eval_runner = app_root / "app" / "modules" / "library" / "runtime" / "run_meta_evaluate.py"
    norm_refresh_runner = app_root / "app" / "modules" / "library" / "runtime" / "run_normalization_refresh.py"
    py_bootstrap = 'PY_BIN=".venv/bin/python"; [ -x "$PY_BIN" ] || PY_BIN="python3"; '
    sync_cmd = py_bootstrap + f'"$PY_BIN" "{sync_runner}"'
    meta_eval_cmd = py_bootstrap + f'"$PY_BIN" "{meta_eval_runner}" --workers 1'
    personality_refresh_cmd = (
        py_bootstrap + f'"$PY_BIN" "{norm_refresh_runner}" --entity-type personality --limit 180'
    )
    publisher_refresh_cmd = (
        py_bootstrap + f'"$PY_BIN" "{norm_refresh_runner}" --entity-type publisher --limit 180'
    )

    return [
        {
            "task_id": MONOCORPUS_SYNC_TASK_ID,
            "panel_id": "maintenance",
            "title": "Monocorpus sync",
            "task_type": "sync",
            "icon_idle": "RefreshCw",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": sync_cmd},
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
