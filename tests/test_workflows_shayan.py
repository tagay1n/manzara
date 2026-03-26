"""Workflow service tests for Shayan context updates."""

from __future__ import annotations

from app.workflows import WorkflowService


class _DummyDb:
    pass


class _DummyRunner:
    pass


def test_collect_post_state_uses_scan_artifacts_from_run_summary() -> None:
    service = WorkflowService(_DummyDb(), _DummyRunner())
    output, updates = service._collect_post_state(
        "shayan.scan_changes",
        pre_state={},
        task_run={
            "status": "completed",
            "summary": {
                "artifacts": {
                    "kind": "shayan.snapshot_diff",
                    "episodes_before": 10,
                    "episodes_after": 13,
                    "episodes_added": 3,
                    "episodes_changed": 2,
                    "episodes_removed": 0,
                }
            },
        },
    )
    assert output["scan_before_count"] == 10
    assert output["scan_after_count"] == 13
    assert output["scan_new_items_count"] == 3
    assert output["scan_changed_items_count"] == 2
    assert updates["scan_new_items_count"] == 3
