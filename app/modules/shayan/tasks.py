"""Task definitions for the Shayan module."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Dict, List

from app.modules.shayan.config import ShayanSettings


def shayan_task_definitions(shayan: ShayanSettings) -> List[Dict[str, Any]]:
    """Return Shayan task definitions for dashboard and runtime."""
    app_root = Path(__file__).resolve().parents[3]
    py_bootstrap = 'PY_BIN=".venv/bin/python"; [ -x "$PY_BIN" ] || PY_BIN="python3"; '
    repo_path = shlex.quote(str(shayan.repo_path))
    output_path = shlex.quote(str(shayan.output_path))

    scan_cmd = (
        py_bootstrap
        + '"$PY_BIN" -m app.modules.shayan.runtime.run_stage'
        + " --stage scan_changes"
        + f" --repo-path {repo_path}"
        + f" --output-path {output_path}"
    )
    download_cmd = (
        py_bootstrap
        + '"$PY_BIN" -m app.modules.shayan.runtime.run_stage'
        + " --stage download_new"
        + f" --repo-path {repo_path}"
        + f" --output-path {output_path}"
    )
    upload_cmd = (
        py_bootstrap
        + '"$PY_BIN" -m app.modules.shayan.runtime.run_stage'
        + " --stage upload_yadisk"
        + f" --repo-path {repo_path}"
        + f" --output-path {output_path}"
    )
    transfer_cmd = (
        py_bootstrap
        + '"$PY_BIN" -m app.modules.shayan.runtime.transfer_yadisk_webdav'
    )

    return [
        {
            "task_id": "shayan.scan_changes",
            "panel_id": "shayan",
            "title": "Scan for changes",
            "task_type": "scan",
            "icon_idle": "RefreshCw",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": scan_cmd},
        },
        {
            "task_id": "shayan.download_new",
            "panel_id": "shayan",
            "title": "Download new",
            "task_type": "download",
            "icon_idle": "Play",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": download_cmd},
        },
        {
            "task_id": "shayan.upload_yadisk",
            "panel_id": "shayan",
            "title": "Upload to Yandex Disk",
            "task_type": "upload",
            "icon_idle": "CloudUpload",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": upload_cmd},
        },
        {
            "task_id": "shayan.transfer_yadisk_webdav",
            "panel_id": "shayan",
            "title": "Migrate to Hetzner",
            "task_type": "transfer",
            "icon_idle": "CloudCog",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": transfer_cmd},
        },
    ]
