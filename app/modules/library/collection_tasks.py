"""Task definitions owned by the Collections flow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.modules.library.collection_constants import (
    COLLECTIONS_PANEL_ID,
    COLLECTION_APPLY_TASK_ID,
    COLLECTION_DETECT_TASK_ID,
    COLLECTION_VALIDATE_TASK_ID,
)


def collection_task_definitions(
    *, app_root: Path | None = None
) -> list[dict[str, Any]]:
    """Return the ordered collection curation task catalog."""
    root = app_root or Path(__file__).resolve().parents[3]
    runtime = root / "app" / "modules" / "library" / "runtime"
    py_bootstrap = 'PY_BIN=".venv/bin/python"; [ -x "$PY_BIN" ] || PY_BIN="python3"; '
    return [
        {
            "task_id": COLLECTION_DETECT_TASK_ID,
            "panel_id": COLLECTIONS_PANEL_ID,
            "title": "Discover collections",
            "task_type": "metadata",
            "icon_idle": "Folders",
            "icon_running": "Square",
            "cwd": str(root),
            "command": {
                "mode": "shell",
                "value": py_bootstrap
                + f'"$PY_BIN" "{runtime / "run_collection_detect.py"}"',
            },
        },
        {
            "task_id": COLLECTION_VALIDATE_TASK_ID,
            "panel_id": COLLECTIONS_PANEL_ID,
            "title": "Validate collection proposals",
            "task_type": "metadata",
            "icon_idle": "ScanSearch",
            "icon_running": "Square",
            "cwd": str(root),
            "command": {
                "mode": "shell",
                "value": py_bootstrap
                + f'"$PY_BIN" "{runtime / "run_collection_validate.py"}"',
            },
        },
        {
            "task_id": COLLECTION_APPLY_TASK_ID,
            "panel_id": COLLECTIONS_PANEL_ID,
            "title": "Apply collection overrides",
            "task_type": "metadata",
            "icon_idle": "CheckCheck",
            "icon_running": "Square",
            "cwd": str(root),
            "command": {
                "mode": "shell",
                "value": py_bootstrap
                + f'"$PY_BIN" "{runtime / "run_collection_apply.py"}" --limit 500',
            },
        },
    ]


__all__ = ["collection_task_definitions"]
