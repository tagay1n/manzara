"""Panel payload builder for the Maintenance module."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from app.db import Database
from app.modules.maintenance.config import MaintenanceSettings
from app.modules.maintenance.tasks import (
    MAINTENANCE_PGBACKREST_FULL_TASK_ID,
    MAINTENANCE_PGBACKREST_INCR_TASK_ID,
)
from app.modules.maintenance.workflow import (
    MAINTENANCE_BACKUP_FULL_SCHEDULE_ID,
    MAINTENANCE_BACKUP_INCR_SCHEDULE_ID,
)


def _sum_counts(counts: Dict[str, int]) -> int:
    return sum(int(value) for value in counts.values())


def _last_run_for_panel(db: Database, panel_id: str) -> Dict[str, Any] | None:
    for run in db.list_recent_runs(limit=100):
        if run.get("panel_id") == panel_id:
            return run
    return None


def _backup_task_state(
    db: Database,
    *,
    task_id: str,
    schedule_id: str,
) -> Dict[str, Any]:
    run = (db.list_recent_runs_for_task(task_id, limit=1) or [None])[0]
    schedule = db.get_schedule(schedule_id)
    return {
        "task_id": task_id,
        "run": {
            "run_id": int(run["run_id"]) if run else None,
            "status": str(run.get("status") or "idle") if run else "idle",
            "started_at": run.get("started_at") if run else None,
            "finished_at": run.get("finished_at") if run else None,
            "exit_code": run.get("exit_code") if run else None,
            "error_text": run.get("error_text") if run else None,
        },
        "schedule": {
            "schedule_id": schedule.get("schedule_id") if schedule else schedule_id,
            "enabled": bool(schedule.get("enabled")) if schedule else False,
            "next_run_at": schedule.get("next_run_at") if schedule else None,
            "last_run_at": schedule.get("last_run_at") if schedule else None,
        },
    }


def _disk_warning_level(
    *,
    free_bytes: int,
    total_bytes: int,
    database_size_bytes: int,
) -> tuple[str, str]:
    free_pct = (float(free_bytes) / float(total_bytes) * 100.0) if total_bytes > 0 else 0.0
    warn_floor = max(10 * 1024**3, int(database_size_bytes * 2))
    crit_floor = max(5 * 1024**3, int(database_size_bytes))
    if free_bytes < crit_floor or free_pct < 7.5:
        return ("critical", "Free space is critically low relative to database size.")
    if free_bytes < warn_floor or free_pct < 15.0:
        return ("warn", "Free space may become insufficient soon.")
    return ("ok", "Free space looks sufficient.")


def build_database_state_snapshot(db: Database) -> Dict[str, Any]:
    """Build one diagnostics snapshot for database/disk/backup visibility."""
    captured_at = datetime.now(timezone.utc).isoformat()
    try:
        storage = db.get_database_storage_snapshot(schema_name=db.schema, table_limit=250)
        data_directory = str(storage.get("data_directory") or "").strip()
        disk_info = None
        if data_directory:
            disk_probe = Path(data_directory)
            disk = shutil.disk_usage(disk_probe)
            warning_level, warning_text = _disk_warning_level(
                free_bytes=int(disk.free),
                total_bytes=int(disk.total),
                database_size_bytes=int(storage.get("database_size_bytes") or 0),
            )
            disk_info = {
                "path": str(disk_probe),
                "total_bytes": int(disk.total),
                "used_bytes": int(disk.used),
                "free_bytes": int(disk.free),
                "free_pct": round(float(disk.free) / float(disk.total) * 100.0, 2)
                if disk.total > 0
                else 0.0,
                "warning_level": warning_level,
                "warning_text": warning_text,
            }
        backup_info = {
            "full": _backup_task_state(
                db,
                task_id=MAINTENANCE_PGBACKREST_FULL_TASK_ID,
                schedule_id=MAINTENANCE_BACKUP_FULL_SCHEDULE_ID,
            ),
            "incremental": _backup_task_state(
                db,
                task_id=MAINTENANCE_PGBACKREST_INCR_TASK_ID,
                schedule_id=MAINTENANCE_BACKUP_INCR_SCHEDULE_ID,
            ),
        }
        return {
            "available": True,
            "error": None,
            "captured_at": captured_at,
            "database_name": storage.get("database_name"),
            "schema": storage.get("schema"),
            "database_size_bytes": int(storage.get("database_size_bytes") or 0),
            "data_directory": data_directory or None,
            "disk": disk_info,
            "tables": storage.get("tables") or [],
            "backup": backup_info,
        }
    except Exception as exc:  # pragma: no cover - runtime fallback
        return {
            "available": False,
            "error": str(exc),
            "captured_at": captured_at,
            "database_name": None,
            "schema": db.schema,
            "database_size_bytes": 0,
            "data_directory": None,
            "disk": None,
            "tables": [],
            "backup": {
                "full": _backup_task_state(
                    db,
                    task_id=MAINTENANCE_PGBACKREST_FULL_TASK_ID,
                    schedule_id=MAINTENANCE_BACKUP_FULL_SCHEDULE_ID,
                ),
                "incremental": _backup_task_state(
                    db,
                    task_id=MAINTENANCE_PGBACKREST_INCR_TASK_ID,
                    schedule_id=MAINTENANCE_BACKUP_INCR_SCHEDULE_ID,
                ),
            },
        }


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
    *,
    title: str = "Yandex disk",
) -> Dict[str, Any]:
    """Build dashboard panel payload for maintenance tasks."""
    return _build_ops_panel(
        db=db,
        maintenance=maintenance,
        panel_id="maintenance",
        title=title,
        description="Yandex Disk synchronization, migration, and cleanup.",
        tasks=tasks,
    )


def build_backup_panel(
    db: Database,
    maintenance: MaintenanceSettings,
    tasks: List[Dict[str, Any]],
    *,
    title: str = "Backup",
) -> Dict[str, Any]:
    """Build dashboard panel payload for database backup tasks."""
    return _build_ops_panel(
        db=db,
        maintenance=maintenance,
        panel_id="backup",
        title=title,
        description="PostgreSQL backup operations and schedules.",
        tasks=tasks,
    )


def build_library_panel(
    db: Database,
    maintenance: MaintenanceSettings,
    tasks: List[Dict[str, Any]],
    *,
    title: str = "Library",
) -> Dict[str, Any]:
    """Build dashboard panel payload for library-related tasks."""
    return _build_ops_panel(
        db=db,
        maintenance=maintenance,
        panel_id="library",
        title=title,
        description="Metadata and curation workflows.",
        tasks=tasks,
    )
