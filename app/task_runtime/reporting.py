"""Structured stdout results shared by scheduled workflow runners."""

import json
from typing import Any

from app.task_runtime.logging import redact, write_stdout


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
