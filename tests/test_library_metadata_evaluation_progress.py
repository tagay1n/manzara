"""Library metadata evaluation progress coverage."""

from __future__ import annotations

from app.modules.library.runtime.metadata.evaluation_progress import _EvaluationProgress


def test_evaluation_progress_matches_metadata_extraction_contract() -> None:
    class _Db:
        def __init__(self) -> None:
            self.progress: list[dict] = []
            self.events: list[tuple[str, dict]] = []

        def publish_run_progress(self, **kwargs):  # noqa: ANN003
            self.progress.append(kwargs["progress"])
            self.events.append(("task.progress", kwargs))

    db = _Db()
    progress = _EvaluationProgress(db, run_id=42, total=2)

    progress.publish()
    progress.record_model_attempt("model-first")
    progress.record_completed("succeeded", model_name="model-first")
    progress.record_completed("rules_skipped")

    assert db.progress[-1] == {
        "current": 2,
        "total": 2,
        "percent": 100.0,
        "remaining": 0,
        "succeeded": 1,
        "rules_skipped": 1,
        "terminal": 0,
        "quota_deferred": 0,
        "service_deferred": 0,
        "model_attempts": {"model-first": 1},
        "model_successes": {"model-first": 1},
    }
    assert db.events[-1][0] == "task.progress"
