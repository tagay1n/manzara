"""Unit tests for shared runtime state machines."""

from __future__ import annotations

import pytest

from app.runtime_states import (
    TASK_RUN_STATUS_COMPLETED,
    TASK_RUN_STATUS_FAILED,
    TASK_RUN_STATUS_RUNNING,
    TASK_RUN_STATUS_STOPPED,
    TASK_RUN_STATUS_STOPPING_FORCE,
    WORKFLOW_RUN_STATUS_COMPLETED,
    WORKFLOW_RUN_STATUS_FAILED,
    WORKFLOW_RUN_STATUS_RUNNING,
    can_transition_task_run,
    can_transition_workflow_run,
    resolve_task_terminal_status,
    task_status_from_stop_mode,
    task_terminal_event_type,
)


def test_task_transition_rules_cover_running_and_terminal_paths() -> None:
    assert can_transition_task_run(TASK_RUN_STATUS_RUNNING, TASK_RUN_STATUS_STOPPING_FORCE)
    assert can_transition_task_run(TASK_RUN_STATUS_RUNNING, TASK_RUN_STATUS_COMPLETED)
    assert can_transition_task_run(TASK_RUN_STATUS_STOPPING_FORCE, TASK_RUN_STATUS_STOPPED)
    assert can_transition_task_run(TASK_RUN_STATUS_RUNNING, TASK_RUN_STATUS_FAILED)
    assert not can_transition_task_run(TASK_RUN_STATUS_COMPLETED, TASK_RUN_STATUS_RUNNING)


def test_workflow_transition_rules_cover_running_and_terminal_paths() -> None:
    assert can_transition_workflow_run(WORKFLOW_RUN_STATUS_RUNNING, WORKFLOW_RUN_STATUS_COMPLETED)
    assert can_transition_workflow_run(WORKFLOW_RUN_STATUS_RUNNING, WORKFLOW_RUN_STATUS_FAILED)
    assert not can_transition_workflow_run(
        WORKFLOW_RUN_STATUS_COMPLETED,
        WORKFLOW_RUN_STATUS_RUNNING,
    )


def test_task_terminal_status_resolution() -> None:
    assert resolve_task_terminal_status(exit_code=0, stop_mode=None) == TASK_RUN_STATUS_COMPLETED
    assert resolve_task_terminal_status(exit_code=7, stop_mode=None) == TASK_RUN_STATUS_FAILED
    assert resolve_task_terminal_status(exit_code=0, stop_mode="graceful") == TASK_RUN_STATUS_STOPPED


def test_task_stop_mode_to_status_mapping() -> None:
    assert task_status_from_stop_mode("graceful") == "stopping_graceful"
    assert task_status_from_stop_mode("force") == TASK_RUN_STATUS_STOPPING_FORCE


def test_task_terminal_event_mapping() -> None:
    assert task_terminal_event_type(TASK_RUN_STATUS_COMPLETED) == "task.completed"
    assert task_terminal_event_type(TASK_RUN_STATUS_STOPPED) == "task.stopped"
    assert task_terminal_event_type(TASK_RUN_STATUS_FAILED) == "task.failed"
    with pytest.raises(ValueError):
        task_terminal_event_type("running")

