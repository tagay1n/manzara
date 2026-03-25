"""Panel payload builder for the Oscar module."""

from __future__ import annotations

from typing import Any, Dict, List

from app.db import Database
from app.modules.oscar.config import OscarSettings


def _sum_counts(counts: Dict[str, int]) -> int:
    return sum(int(value) for value in counts.values())


def _last_run_for_panel(db: Database, panel_id: str) -> Dict[str, Any] | None:
    for run in db.list_recent_runs(limit=100):
        if run.get("panel_id") == panel_id:
            return run
    return None


def build_oscar_panel(
    db: Database,
    oscar: OscarSettings,
    tasks: List[Dict[str, Any]],
    *,
    title: str = "Oscar",
) -> Dict[str, Any]:
    """Build dashboard panel payload for Oscar."""
    panel_id = "oscar"
    counts = db.run_count_by_status(panel_id)
    total_runs = _sum_counts(counts)
    last_run = _last_run_for_panel(db, panel_id)

    stats_cards = [
        {"label": "Total Runs", "value": str(total_runs)},
        {
            "label": "Last Status",
            "value": str((last_run or {}).get("status") or "-").replace("_", " "),
        },
        {"label": "Last Success", "value": db.last_successful_run(panel_id) or "-"},
        {"label": "Repo Exists", "value": "yes" if oscar.repo_path.exists() else "no"},
        {
            "label": "Parquet Part",
            "value": f"{int(oscar.parquet_part_size_mb)} MB",
        },
        {
            "label": "Artifacts Dir",
            "value": "yes" if oscar.artifacts_dir.exists() else "no",
        },
    ]

    return {
        "panel_id": panel_id,
        "title": title,
        "description": "Snapshot pipeline skeleton (resolve offsets -> download ranges -> export parquet).",
        "status_counts": counts,
        "stats_cards": stats_cards,
        "tasks": tasks,
    }

