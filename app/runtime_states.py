"""Shared runtime state machines for task and workflow runs."""

from __future__ import annotations

from typing import Mapping, Set

TASK_RUN_STATUS_STARTING = "starting"
TASK_RUN_STATUS_RUNNING = "running"
TASK_RUN_STATUS_STOPPING_GRACEFUL = "stopping_graceful"
TASK_RUN_STATUS_STOPPING_FORCE = "stopping_force"
TASK_RUN_STATUS_STOPPED = "stopped"
TASK_RUN_STATUS_COMPLETED = "completed"
TASK_RUN_STATUS_FAILED = "failed"

TASK_RUN_ACTIVE_STATUSES = (
    TASK_RUN_STATUS_STARTING,
    TASK_RUN_STATUS_RUNNING,
    TASK_RUN_STATUS_STOPPING_GRACEFUL,
    TASK_RUN_STATUS_STOPPING_FORCE,
)

TASK_RUN_TERMINAL_STATUSES = (
    TASK_RUN_STATUS_STOPPED,
    TASK_RUN_STATUS_COMPLETED,
    TASK_RUN_STATUS_FAILED,
)

TASK_RUN_TRANSITIONS: Mapping[str, Set[str]] = {
    TASK_RUN_STATUS_STARTING: {
        TASK_RUN_STATUS_RUNNING,
        TASK_RUN_STATUS_FAILED,
    },
    TASK_RUN_STATUS_RUNNING: {
        TASK_RUN_STATUS_STOPPING_GRACEFUL,
        TASK_RUN_STATUS_STOPPING_FORCE,
        TASK_RUN_STATUS_STOPPED,
        TASK_RUN_STATUS_COMPLETED,
        TASK_RUN_STATUS_FAILED,
    },
    TASK_RUN_STATUS_STOPPING_GRACEFUL: {
        TASK_RUN_STATUS_STOPPING_FORCE,
        TASK_RUN_STATUS_STOPPED,
        TASK_RUN_STATUS_FAILED,
    },
    TASK_RUN_STATUS_STOPPING_FORCE: {
        TASK_RUN_STATUS_STOPPED,
        TASK_RUN_STATUS_FAILED,
    },
    TASK_RUN_STATUS_STOPPED: set(),
    TASK_RUN_STATUS_COMPLETED: set(),
    TASK_RUN_STATUS_FAILED: set(),
}

WORKFLOW_RUN_STATUS_STARTING = "starting"
WORKFLOW_RUN_STATUS_RUNNING = "running"
WORKFLOW_RUN_STATUS_STOPPED = "stopped"
WORKFLOW_RUN_STATUS_COMPLETED = "completed"
WORKFLOW_RUN_STATUS_FAILED = "failed"

WORKFLOW_RUN_ACTIVE_STATUSES = (
    WORKFLOW_RUN_STATUS_STARTING,
    WORKFLOW_RUN_STATUS_RUNNING,
)

WORKFLOW_RUN_TERMINAL_STATUSES = (
    WORKFLOW_RUN_STATUS_STOPPED,
    WORKFLOW_RUN_STATUS_COMPLETED,
    WORKFLOW_RUN_STATUS_FAILED,
)

WORKFLOW_RUN_TRANSITIONS: Mapping[str, Set[str]] = {
    WORKFLOW_RUN_STATUS_STARTING: {
        WORKFLOW_RUN_STATUS_RUNNING,
        WORKFLOW_RUN_STATUS_FAILED,
    },
    WORKFLOW_RUN_STATUS_RUNNING: {
        WORKFLOW_RUN_STATUS_STOPPED,
        WORKFLOW_RUN_STATUS_COMPLETED,
        WORKFLOW_RUN_STATUS_FAILED,
    },
    WORKFLOW_RUN_STATUS_STOPPED: set(),
    WORKFLOW_RUN_STATUS_COMPLETED: set(),
    WORKFLOW_RUN_STATUS_FAILED: set(),
}

TASK_TERMINAL_EVENT_TYPES = {
    TASK_RUN_STATUS_STOPPED: "task.stopped",
    TASK_RUN_STATUS_COMPLETED: "task.completed",
    TASK_RUN_STATUS_FAILED: "task.failed",
}


def can_transition_task_run(current: str, target: str) -> bool:
    """Return True when a task run status transition is allowed."""
    current_value = str(current or "")
    target_value = str(target or "")
    if current_value == target_value:
        return True
    return target_value in TASK_RUN_TRANSITIONS.get(current_value, set())


def can_transition_workflow_run(current: str, target: str) -> bool:
    """Return True when a workflow run status transition is allowed."""
    current_value = str(current or "")
    target_value = str(target or "")
    if current_value == target_value:
        return True
    return target_value in WORKFLOW_RUN_TRANSITIONS.get(current_value, set())


def task_status_from_stop_mode(mode: str) -> str:
    """Convert stop mode to the corresponding transient task status."""
    return (
        TASK_RUN_STATUS_STOPPING_GRACEFUL
        if str(mode or "") == "graceful"
        else TASK_RUN_STATUS_STOPPING_FORCE
    )


def resolve_task_terminal_status(*, exit_code: int, stop_mode: str | None) -> str:
    """Resolve terminal task status from process outcome and stop mode."""
    if stop_mode is not None:
        return TASK_RUN_STATUS_STOPPED
    if int(exit_code) == 0:
        return TASK_RUN_STATUS_COMPLETED
    return TASK_RUN_STATUS_FAILED


def task_terminal_event_type(status: str) -> str:
    """Return event name for a terminal task status."""
    value = str(status or "")
    if value not in TASK_TERMINAL_EVENT_TYPES:
        raise ValueError(f"Unsupported terminal status for event mapping: {value!r}")
    return TASK_TERMINAL_EVENT_TYPES[value]

