"""Application startup/shutdown bootstrap helpers."""

from __future__ import annotations

from typing import Any, Dict, List


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

    if state.settings.scheduler_enabled:
        state.workflow_service.start()


def shutdown_app(*, state: Any) -> None:
    """Stop scheduler and mark runtime as shutting down."""
    state.shutting_down = True
    state.workflow_service.stop()
