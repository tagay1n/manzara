"""Application startup/shutdown bootstrap helpers."""

from __future__ import annotations

from typing import Any, Dict, List

from app.modules.shayan.state_migration import migrate_legacy_shayan_state_if_needed


def startup_app(
    *,
    state: Any,
    panel_defs: List[Dict[str, Any]],
    task_defs: List[Dict[str, Any]],
) -> None:
    """Initialize schema, seed runtime definitions, and recover runs."""
    state.shutting_down = False
    db = state.db
    db.init_schema()
    db.seed_panels(panel_defs)
    db.seed_tasks(task_defs)
    db.prune_runtime_definitions(
        panel_ids=[str(item.get("panel_id") or "") for item in panel_defs],
        task_ids=[str(item.get("task_id") or "") for item in task_defs],
    )

    try:
        migration = migrate_legacy_shayan_state_if_needed(
            db,
            artifacts_dir=state.settings.shayan.artifacts_dir,
        )
        if migration.get("migrated"):
            db.insert_event(
                "shayan.state_migrated",
                task_id=None,
                run_id=None,
                panel_id="shayan",
                payload=migration,
            )
    except Exception as exc:
        db.insert_event(
            "shayan.state_migration_failed",
            task_id=None,
            run_id=None,
            panel_id="shayan",
            payload={"error": str(exc)},
        )

    recovered_runs = db.recover_active_runs()
    if recovered_runs > 0:
        db.insert_event(
            "system.recovery",
            task_id=None,
            run_id=None,
            panel_id=None,
            payload={"recovered_runs": recovered_runs},
        )

    recovered_conveyors = db.recover_active_conveyor_runs()
    if recovered_conveyors > 0:
        db.insert_event(
            "system.conveyor_recovery",
            task_id=None,
            run_id=None,
            panel_id=None,
            payload={"recovered_conveyor_runs": recovered_conveyors},
        )


def shutdown_app(*, state: Any) -> None:
    """Mark runtime as shutting down."""
    state.shutting_down = True
