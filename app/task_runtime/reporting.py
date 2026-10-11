"""Final-result presentation shared by terminal and scheduled runners."""

import json
from typing import Any

from app.runtime_states import TASK_RUN_STATUS_COMPLETED, TASK_RUN_STATUS_FAILED
from app.task_runtime.logging import redact, write_stdout


COMPLETION_BELL_INTERVAL_SECONDS = 0.2


def completion_bell_count(run: dict[str, Any] | None, *, failure: str | None = None) -> int:
    """One bell for success, two for failure, silence for deferred/stopped work."""
    summary = (run or {}).get("summary") or {}
    outcome = summary.get("outcome", "completed")
    if failure or (run or {}).get("status") == TASK_RUN_STATUS_FAILED or outcome == "failed":
        return 2
    if (run and run.get("status") == TASK_RUN_STATUS_COMPLETED
            and run.get("exit_code") == 0 and outcome == "completed"):
        return 1
    return 0


def run_result(run: dict[str, Any]) -> dict[str, Any]:
    """Project a persisted run into the workflow result contract."""
    summary = run.get("summary") or {}
    result = {
        "task_id": run["task_id"], "run_id": run["run_id"], "status": run["status"],
        "exit_code": run.get("exit_code"),
        "counters": {key: value for key, value in summary.items() if type(value) is int},
    }
    if run.get("error_text") or summary.get("error"):
        result["error"] = redact(run.get("error_text") or summary["error"])
    return result


def report_run(run: dict[str, Any], results: list[dict[str, Any]]) -> None:
    result = run_result(run)
    results.append(result)
    write_stdout(json.dumps(result, ensure_ascii=True) + "\n")
