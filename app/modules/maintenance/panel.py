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


def _build_ops_panel(
    db: Database,
    maintenance: MaintenanceSettings,
    *,
    panel_id: str,
    title: str,
    description: str,
    tasks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build dashboard payload for non-shayan operations panels."""
    counts = db.run_count_by_status(panel_id)
    total_runs = _sum_counts(counts)
    last_run = _last_run_for_panel(db, panel_id)

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
            "value": db.last_successful_run(panel_id) or "-",
        },
        {
            "label": "Repo Exists",
            "value": "yes" if maintenance.monocorpus_repo_path.exists() else "no",
        },
    ]

    return {
        "panel_id": panel_id,
        "title": title,
        "description": description,
        "status_counts": counts,
        "stats_cards": stats_cards,
        "tasks": tasks,
    }


def build_maintenance_panel(
    db: Database,
    maintenance: MaintenanceSettings,
    tasks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build dashboard panel payload for maintenance tasks."""
    return _build_ops_panel(
        db=db,
        maintenance=maintenance,
        panel_id="maintenance",
        title="Maintenance",
        description="Repository operations and health checks.",
        tasks=tasks,
    )


def build_library_panel(
    db: Database,
    maintenance: MaintenanceSettings,
    tasks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build dashboard panel payload for library-related tasks."""
    return _build_ops_panel(
        db=db,
        maintenance=maintenance,
        panel_id="library",
        title="Library",
        description="Metadata and curation workflows.",
        tasks=tasks,
    )
