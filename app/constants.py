"""Centralized application constants."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_ROOT / "static"

SSE_POLL_INTERVAL_SECONDS = 1.0
SSE_HEARTBEAT_EVERY_EMPTY_POLLS = 15
TITLE_MAX_LENGTH = 80

SLUG_SEPARATOR_PATTERN = re.compile(r"[\s_]+")
SLUG_CLEAN_PATTERN = re.compile(r"[^\w-]+", flags=re.UNICODE)

PANEL_DEFS: list[dict[str, Any]] = [
    {"panel_id": "shayan", "title": "Shayan"},
    {"panel_id": "maintenance", "title": "Maintenance"},
    {"panel_id": "backup", "title": "Backup"},
    {"panel_id": "library", "title": "Library"},
    {"panel_id": "collections", "title": "Collections"},
]
