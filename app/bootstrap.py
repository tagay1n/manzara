"""Application startup/shutdown bootstrap helpers."""

from __future__ import annotations

from typing import Any, Dict, List

from app.modules.shayan.state_migration import migrate_legacy_shayan_state_if_needed


def startup_app(
    *,
    state: Any,
    panel_defs: List[Dict[str, Any]],
    task_defs: List[Dict[str, Any]],
    workflow_bundles: List[Dict[str, Any]],
) -> None:
    """Initialize schema, seed runtime definitions, recover runs, and start scheduler."""
    state.shutting_down = False
    db = state.db
    db.init_schema()
    db.seed_panels(panel_defs)
    db.seed_tasks(task_defs)
    for bundle in workflow_bundles:
        db.seed_workflow_bundle(bundle)
    db.prune_runtime_definitions(
        panel_ids=[str(item.get("panel_id") or "") for item in panel_defs],
        task_ids=[str(item.get("task_id") or "") for item in task_defs],
        workflow_ids=[
            str((bundle.get("workflow") or {}).get("workflow_id") or "")
            for bundle in workflow_bundles
        ],
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

    recovered_workflows = db.recover_active_workflow_runs()
    if recovered_workflows > 0:
        db.insert_event(
            "system.workflow_recovery",
            task_id=None,
            run_id=None,
            panel_id=None,
            payload={"recovered_workflow_runs": recovered_workflows},
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

    if state.settings.scheduler_enabled:
        state.workflow_service.start()


def shutdown_app(*, state: Any) -> None:
    """Stop scheduler and mark runtime as shutting down."""
    state.shutting_down = True
    state.workflow_service.stop()
