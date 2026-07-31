"""Task definitions owned by the Library flow."""

from __future__ import annotations

from pathlib import Path
from typing import Any


LIBRARY_GENERATE_BOOK_PREVIEWS_TASK_ID = "library.generate_book_previews"


def library_task_definitions(*, app_root: Path | None = None) -> list[dict[str, Any]]:
    """Return task definitions implemented by the Library module."""
    root = app_root or Path(__file__).resolve().parents[3]
    runner = root / "app" / "modules" / "library" / "runtime" / "run_generate_book_previews.py"
    py_bootstrap = 'PY_BIN=".venv/bin/python"; [ -x "$PY_BIN" ] || PY_BIN="python3"; '
    return [
        {
            "task_id": LIBRARY_GENERATE_BOOK_PREVIEWS_TASK_ID,
            "panel_id": "library",
            "title": "Generate book previews",
            "task_type": "preview",
            "icon_idle": "Images",
            "icon_running": "Square",
            "cwd": str(root),
            "command": {
                "mode": "shell",
                "value": py_bootstrap + f'"$PY_BIN" "{runner}"',
            },
        }
    ]


__all__ = ["LIBRARY_GENERATE_BOOK_PREVIEWS_TASK_ID", "library_task_definitions"]
