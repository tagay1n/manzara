"""Panel payload builder for the Maintenance module."""

from __future__ import annotations

from typing import Any, Dict, List

from app.db import Database
from app.modules.maintenance.config import MaintenanceSettings


def _sum_counts(counts: Dict[str, int]) -> int:
    return sum(int(value) for value in counts.values())


def _last_run_for_panel(db: Database, panel_id: str) -> Dict[str, Any] | None:
    for run in db.list_recent_runs(limit=100):
        if run.get("panel_id") == panel_id:
            return run
    return None


def build_maintenance_panel(
    db: Database,
    maintenance: MaintenanceSettings,
    tasks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build dashboard panel payload for maintenance tasks."""
    counts = db.run_count_by_status("maintenance")
    total_runs = _sum_counts(counts)
    last_run = _last_run_for_panel(db, "maintenance")

    stats_cards = [
        {
            "label": "Total Runs",
            "value": str(total_runs),
        },
        {
            "label": "Last Status",
            "value": str((last_run or {}).get("status") or "-").replace("_", " "),
        },
        {
            "label": "Last Success",
            "value": db.last_successful_run("maintenance") or "-",
        },
        {
            "label": "Repo Exists",
            "value": "yes" if maintenance.monocorpus_repo_path.exists() else "no",
        },
    ]

    return {
        "panel_id": "maintenance",
        "title": "Maintenance",
        "description": "Operational tasks for auxiliary repositories.",
        "status_counts": counts,
        "stats_cards": stats_cards,
        "tasks": tasks,
    }
