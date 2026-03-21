"""Persistence helpers for sync wiping plan."""

from __future__ import annotations

import json
import os

from core.paths import get_in_workdir
from dirs import Dirs


def get_wiping_plan() -> dict[str, str]:
    """Load or create the JSON plan of documents to wipe/move."""
    marked_for_wiping = get_in_workdir(Dirs.WIPING_PLAN, file="marked_for_wiping.json")
    if not os.path.exists(marked_for_wiping):
        print("No marked for wiping file found, creating a new one")
        with open(marked_for_wiping, 'w') as f:
            json.dump({}, f)
    with open(marked_for_wiping, 'r') as f:
        return json.load(f)


def flush(plan: dict[str, str]) -> None:
    """Persist the wiping plan JSON to disk."""
    marked_for_wiping = get_in_workdir(Dirs.WIPING_PLAN, file="marked_for_wiping.json")
    with open(marked_for_wiping, 'w') as f:
        json.dump(plan, f, indent=4, ensure_ascii=False)
