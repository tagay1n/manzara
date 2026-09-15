"""Evaluation batch coordination without storage or live Gemini requests."""

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.gemini_runtime import GeminiRuntimeError
from app.modules.library.runtime.metadata import evaluation, evaluation_channel


@pytest.fixture
def batch_runtime(monkeypatch, tmp_path, evaluation_document):
    progress = []
    created = []
    skipped = []
    db = SimpleNamespace(
        publish_run_progress=lambda **kwargs: progress.append(kwargs["progress"])
    )
    monkeypatch.setattr(evaluation, "read_config", lambda: {})
    monkeypatch.setattr(
        evaluation,
        "load_settings",
        lambda: SimpleNamespace(
            database_url="unused",
            database_schema="test",
            local_state_path=tmp_path / "runtime.sqlite3",
        ),
    )
    monkeypatch.setattr(evaluation, "Database", lambda *_args, **_kwargs: db)
    monkeypatch.setattr(
        evaluation, "load_required_gemini_model_pool", lambda: ["model"]
    )
    monkeypatch.setattr(
        evaluation, "GeminiRuntimeManager", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(evaluation, "_run_id", lambda: 42)
    monkeypatch.setattr(evaluation, "_count_remaining", lambda *_args: 3)
    monkeypatch.setattr(evaluation, "_load_known_classifications", lambda: [])
    monkeypatch.setattr(
        evaluation, "_save_non_applicable", lambda docs: skipped.extend(docs)
    )
    monkeypatch.setattr(
        evaluation_channel, "_unprocessables_dir", lambda: str(tmp_path / "artifacts")
    )
    docs = [
        replace(evaluation_document(), md5="c" * 32, full=False),
        evaluation_document(),
        replace(evaluation_document(), md5="b" * 32),
    ]
    batches = iter([docs, []])
    monkeypatch.setattr(evaluation, "_load_batch", lambda *_args: next(batches))

    class Thread:
        def __init__(self, *, target, name):
            self.target = target
            created.append(name)

        def start(self):
            self.target()

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(
        evaluation, "threading", SimpleNamespace(Thread=Thread, Event=threading.Event)
    )
    monkeypatch.setattr(
        evaluation, "time", SimpleNamespace(sleep=lambda _seconds: None)
    )
    args = SimpleNamespace(batch_size=10, workers=5, dry_run=False, excerpt_chars=0)
    return SimpleNamespace(
        args=args, progress=progress, created=created, skipped=skipped
    )


def test_batch_bounds_workers_to_documents_and_counts_rule_skips(
    batch_runtime, monkeypatch
):
    def make_worker(**kwargs):
        def work():
            kwargs["tasks_queue"].get_nowait()
            kwargs["progress"].record_completed("succeeded", model_name="model")

        return work

    monkeypatch.setattr(evaluation, "LibraryApplicabilityWorker", make_worker)
    evaluation.evaluate(batch_runtime.args)

    assert batch_runtime.created == ["eval-1", "eval-2"]
    assert batch_runtime.skipped == [("c" * 32, "not full")]
    assert batch_runtime.progress[-1]["current"] == 3
    assert batch_runtime.progress[-1]["remaining"] == 0
    assert batch_runtime.progress[-1]["rules_skipped"] == 1
    assert batch_runtime.progress[-1]["succeeded"] == 2


def test_fatal_worker_error_stops_batch_and_surfaces_failure(
    batch_runtime, monkeypatch
):
    stops = []

    def make_worker(**kwargs):
        def work():
            kwargs["channel"].set_fatal_error("invalid Gemini credentials")
            kwargs["stop_event"].set()
            stops.append(kwargs["stop_event"])

        return work

    monkeypatch.setattr(evaluation, "LibraryApplicabilityWorker", make_worker)
    with pytest.raises(GeminiRuntimeError, match="invalid Gemini credentials"):
        evaluation.evaluate(batch_runtime.args)
    assert all(event.is_set() for event in stops)
