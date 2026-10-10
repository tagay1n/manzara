"""Save structured results directly, with references in the run summary."""

from __future__ import annotations

import json
from typing import Any

from app.artifacts import workspace_dir


def save_run_artifact(db, task_id: str, run_id: int, payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("kind"), str) or not payload["kind"]:
        raise ValueError("Structured run artifacts require a kind")
    run = db.get_run(run_id)
    if run is None:
        raise RuntimeError(f"Run {run_id} is missing while saving its artifact")
    summary = run["summary"]
    artifacts = summary.get("artifacts", [])
    root = workspace_dir("task-runs", task_id, run_id=run_id)
    target = root / f"artifact-{len(artifacts) + 1:04d}.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    reference = {"kind": payload["kind"], "artifact_path": str(target)}
    db.update_run_summary(run_id, {**summary, "artifacts": [*artifacts, reference]})
