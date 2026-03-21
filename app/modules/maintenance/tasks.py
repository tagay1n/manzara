"""Task definitions for the Maintenance module."""

from __future__ import annotations

from typing import Any, Dict, List

from app.modules.maintenance.config import MaintenanceSettings


MONOCORPUS_SYNC_TASK_ID = "maintenance.monocorpus_sync"


def maintenance_task_definitions(settings: MaintenanceSettings) -> List[Dict[str, Any]]:
    """Return Maintenance task definitions for dashboard and runtime."""
    py_bootstrap = 'PY_BIN=".venv/bin/python"; [ -x "$PY_BIN" ] || PY_BIN="python3"; '
    sync_cmd = py_bootstrap + '"$PY_BIN" src/main.py sync'

    return [
        {
            "task_id": MONOCORPUS_SYNC_TASK_ID,
            "panel_id": "maintenance",
            "title": "Monocorpus sync",
            "task_type": "sync",
            "icon_idle": "RefreshCw",
            "icon_running": "Square",
            "cwd": str(settings.monocorpus_repo_path),
            "command": {"mode": "shell", "value": sync_cmd},
        },
    ]
