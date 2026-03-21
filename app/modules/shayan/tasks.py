"""Task definitions for the Shayan module."""

from __future__ import annotations

import shlex
from typing import Any, Dict, List

from app.modules.shayan.config import ShayanSettings


def shayan_task_definitions(shayan: ShayanSettings) -> List[Dict[str, Any]]:
    """Return Shayan task definitions for dashboard and runtime."""
    py_bootstrap = 'PY_BIN=".venv/bin/python"; [ -x "$PY_BIN" ] || PY_BIN="python3"; '

    scan_cmd = (
        py_bootstrap
        + '"$PY_BIN" app/main.py snapshot --category all '
        + '--output-file _artifacts/snapshots/latest.json'
    )
    download_cmd = (
        py_bootstrap
        + '"$PY_BIN" app/main.py main --category all '
        + f'--output {shlex.quote(str(shayan.output_path))} '
        + '--status-file _artifacts/status.json '
        + '--summary-file _artifacts/last-main-run-summary.json'
    )

    return [
        {
            "task_id": "shayan.scan_changes",
            "panel_id": "shayan",
            "title": "Scan for changes",
            "task_type": "scan",
            "icon_idle": "RefreshCw",
            "icon_running": "Square",
            "cwd": str(shayan.repo_path),
            "command": {"mode": "shell", "value": scan_cmd},
        },
        {
            "task_id": "shayan.download_new",
            "panel_id": "shayan",
            "title": "Download new",
            "task_type": "download",
            "icon_idle": "Play",
            "icon_running": "Square",
            "cwd": str(shayan.repo_path),
            "command": {"mode": "shell", "value": download_cmd},
        },
    ]
