"""Task definitions for the Oscar module."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Dict, List

from app.modules.oscar.config import OscarSettings


OSCAR_RESOLVE_OFFSETS_TASK_ID = "oscar.resolve_offsets_local"
OSCAR_DOWNLOAD_RANGES_TASK_ID = "oscar.download_ranges"
OSCAR_EXPORT_PARQUET_TASK_ID = "oscar.export_parquet"
OSCAR_DISCOVER_SNAPSHOTS_TASK_ID = "oscar.discover_snapshots"
OSCAR_UPLOAD_DATASET_TASK_ID = "oscar.upload_dataset"


def oscar_task_definitions(settings: OscarSettings) -> List[Dict[str, Any]]:
    """Return Oscar task definitions for dashboard/runtime skeleton."""
    app_root = Path(__file__).resolve().parents[3]

    py_bootstrap = 'PY_BIN=".venv/bin/python"; [ -x "$PY_BIN" ] || PY_BIN="python3"; '
    repo_arg = shlex.quote(str(settings.repo_path))
    artifacts_arg = shlex.quote(str(settings.artifacts_dir))
    base = (
        py_bootstrap
        + '"$PY_BIN" -m app.modules.oscar.runtime.run_stage'
        + f" --repo-path {repo_arg}"
        + f" --artifacts-dir {artifacts_arg}"
    )

    resolve_cmd = base + " --stage resolve_offsets_local"
    download_cmd = base + " --stage download_ranges"
    export_cmd = base + f" --stage export_parquet --part-size-mb {int(settings.parquet_part_size_mb)}"
    discover_cmd = base + " --stage discover_snapshots"
    upload_cmd = base + " --stage upload_dataset"

    return [
        {
            "task_id": OSCAR_DISCOVER_SNAPSHOTS_TASK_ID,
            "panel_id": "oscar",
            "title": "Discover snapshots",
            "task_type": "ingest",
            "icon_idle": "Radar",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": discover_cmd},
        },
        {
            "task_id": OSCAR_RESOLVE_OFFSETS_TASK_ID,
            "panel_id": "oscar",
            "title": "Resolve offsets (local)",
            "task_type": "extract",
            "icon_idle": "Search",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": resolve_cmd},
        },
        {
            "task_id": OSCAR_DOWNLOAD_RANGES_TASK_ID,
            "panel_id": "oscar",
            "title": "Download ranges",
            "task_type": "download",
            "icon_idle": "Download",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": download_cmd},
        },
        {
            "task_id": OSCAR_EXPORT_PARQUET_TASK_ID,
            "panel_id": "oscar",
            "title": "Export parquet",
            "task_type": "export",
            "icon_idle": "Table2",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": export_cmd},
        },
        {
            "task_id": OSCAR_UPLOAD_DATASET_TASK_ID,
            "panel_id": "oscar",
            "title": "Upload dataset",
            "task_type": "publish",
            "icon_idle": "CloudUpload",
            "icon_running": "Square",
            "cwd": str(app_root),
            "command": {"mode": "shell", "value": upload_cmd},
        },
    ]
