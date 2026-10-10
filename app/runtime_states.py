"""Shared task run states and terminal event names."""

from __future__ import annotations


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


TASK_TERMINAL_EVENT_TYPES = {
    TASK_RUN_STATUS_STOPPED: "task.stopped",
    TASK_RUN_STATUS_COMPLETED: "task.completed",
    TASK_RUN_STATUS_FAILED: "task.failed",
}


def task_status_from_stop_mode(mode: str) -> str:
    """Convert stop mode to the corresponding transient task status."""
    return (
        TASK_RUN_STATUS_STOPPING_GRACEFUL
        if str(mode or "") == "graceful"
        else TASK_RUN_STATUS_STOPPING_FORCE
    )


def task_terminal_event_type(status: str) -> str:
    """Return event name for a terminal task status."""
    value = str(status or "")
    if value not in TASK_TERMINAL_EVENT_TYPES:
        raise ValueError(f"Unsupported terminal status for event mapping: {value!r}")
    return TASK_TERMINAL_EVENT_TYPES[value]
