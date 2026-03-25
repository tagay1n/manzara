"""Task definitions for the Shayan module."""

from __future__ import annotations

import shlex
from typing import Any, Dict, List

from app.modules.shayan.config import ShayanSettings


def shayan_task_definitions(shayan: ShayanSettings) -> List[Dict[str, Any]]:
    """Return Shayan task definitions for dashboard and runtime."""
    py_bootstrap = 'PY_BIN=".venv/bin/python"; [ -x "$PY_BIN" ] || PY_BIN="python3"; '
    snapshot_file = shlex.quote(str(shayan.latest_snapshot_file))
    status_file = shlex.quote(str(shayan.status_file))
    summary_file = shlex.quote(str(shayan.summary_file))

    scan_cmd = (
        py_bootstrap
        + '"$PY_BIN" app/main.py snapshot --category all '
        + f"--output-file {snapshot_file}"
    )
    download_cmd = (
        py_bootstrap
        + '"$PY_BIN" app/main.py main --category all '
        + f'--output {shlex.quote(str(shayan.output_path))} '
        + f"--status-file {status_file} "
        + f"--summary-file {summary_file}"
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
