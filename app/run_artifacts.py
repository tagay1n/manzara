"""Task run artifact collection for structured run summaries."""

from __future__ import annotations

from typing import Any, Dict

def capture_pre_run_artifacts(task: Dict[str, Any]) -> Dict[str, Any]:
    """Capture task-specific pre-run state used for post-run diffing."""
    task_id = str(task.get("task_id") or "")
    handler = _PRE_CAPTURE_HANDLERS.get(task_id)
    if handler is None:
        return {}
    return handler(task)


def collect_post_run_artifacts(
    task: Dict[str, Any],
    *,
    status: str,
    pre_state: Dict[str, Any],
    artifact_payload: Dict[str, Any] | None = None,
    log_lines: list[str] | None = None,
) -> Dict[str, Any]:
    """Build task-specific artifact payload for one run."""
    _ = log_lines
    task_id = str(task.get("task_id") or "")
    if status != "completed":
        return {}
    if isinstance(artifact_payload, dict) and artifact_payload:
        return artifact_payload
    handler = _POST_COLLECT_HANDLERS.get(task_id)
    if handler is None:
        return {}
    return handler(task, pre_state)


_PRE_CAPTURE_HANDLERS: Dict[str, Any] = {}

_POST_COLLECT_HANDLERS: Dict[str, Any] = {}
