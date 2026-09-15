"""Library metadata evaluation worker coverage."""

from __future__ import annotations

from queue import Queue
import psycopg2
from app.gemini_model_pool import (
    GeminiModelPoolOperationalError,
    GeminiModelPoolResult,
    GeminiModelPoolUnavailableError,
)
from app.modules.library.runtime.metadata import (
    evaluation_worker as evaluation_worker_module,
)
from app.modules.library.runtime.metadata.evaluation_channel import Channel
from app.modules.library.runtime.metadata.evaluation_progress import _EvaluationProgress
from app.modules.library.runtime.metadata.evaluation_types import Evaluation
from app.modules.library.runtime.metadata.evaluation_worker import (
    LibraryApplicabilityWorker,
)


def test_worker_defers_retryable_service_error_and_continues(
    monkeypatch, evaluation_document
) -> None:
    class _Db:
        def __init__(self) -> None:
            self.progress: list[dict] = []

        def publish_run_progress(self, **kwargs):  # noqa: ANN003
            self.progress.append(kwargs["progress"])

    first = evaluation_document()
    second = evaluation_document()
    second.md5 = "b" * 32
    tasks = Queue()
    tasks.put(first)
    tasks.put(second)
    channel = Channel(dry_run=False)
    db = _Db()
    progress = _EvaluationProgress(db, run_id=42, total=2)
    saved: list[str] = []

    worker = LibraryApplicabilityWorker(
        tasks_queue=tasks,
        config={"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}},
        channel=channel,
        dry_run=False,
        excerpt_chars=0,
        gemini_manager=object(),
        models=["model-first"],
        progress=progress,
    )

    def evaluate(doc, **_kwargs):  # noqa: ANN001
        if doc.md5 == first.md5:
            raise GeminiModelPoolOperationalError("503 pause", retryable=True)
        return GeminiModelPoolResult(
            model_name="model-first",
            value=Evaluation(
                applicable=False,
                reason="not a library document",
            ),
            unavailable_models=(),
        )

    monkeypatch.setattr(evaluation_worker_module, "evaluate_document", evaluate)
    monkeypatch.setattr(
        evaluation_worker_module,
        "save_evaluation_result",
        lambda md5, _evaluation, **_kwargs: saved.append(md5),
    )

    worker()

    assert saved == [second.md5]
    assert channel.get_fatal_error() is None
    assert channel.get_deferred_docs() == {first.md5}
    assert db.progress[-1]["current"] == 2
    assert db.progress[-1]["succeeded"] == 1
    assert db.progress[-1]["service_deferred"] == 1
    assert db.progress[-1]["remaining"] == 1


def test_worker_defers_postgres_outage_without_marking_document_terminal(
    monkeypatch,
    evaluation_document,
) -> None:
    doc = evaluation_document()
    tasks = Queue()
    tasks.put(doc)
    channel = Channel(dry_run=False)
    worker = LibraryApplicabilityWorker(
        tasks_queue=tasks,
        config={"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}},
        channel=channel,
        dry_run=False,
        excerpt_chars=0,
        gemini_manager=object(),
        models=["model-first"],
    )
    terminal_calls: list[str] = []
    monkeypatch.setattr(
        evaluation_worker_module,
        "evaluate_document",
        lambda _doc, **_kwargs: (_ for _ in ()).throw(
            psycopg2.OperationalError("remaining connection slots are reserved")
        ),
    )
    monkeypatch.setattr(
        evaluation_worker_module,
        "mark_evaluation_terminal",
        lambda md5, **_kwargs: terminal_calls.append(md5),
    )

    worker()

    assert channel.get_deferred_docs() == {doc.md5}
    assert terminal_calls == []


def test_worker_stops_cleanly_when_all_models_are_quota_unavailable(
    monkeypatch,
    evaluation_document,
) -> None:
    class _Db:
        def __init__(self) -> None:
            self.progress: list[dict] = []

        def publish_run_progress(self, **kwargs):  # noqa: ANN003
            self.progress.append(kwargs["progress"])

    doc = evaluation_document()
    tasks = Queue()
    tasks.put(doc)
    channel = Channel(dry_run=False)
    db = _Db()
    progress = _EvaluationProgress(db, run_id=42, total=1)
    worker = LibraryApplicabilityWorker(
        tasks_queue=tasks,
        config={"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}},
        channel=channel,
        dry_run=False,
        excerpt_chars=0,
        gemini_manager=object(),
        models=["model-first", "model-second"],
        progress=progress,
    )
    monkeypatch.setattr(
        evaluation_worker_module,
        "evaluate_document",
        lambda _doc, **_kwargs: (_ for _ in ()).throw(
            GeminiModelPoolUnavailableError(["model-first", "model-second"])
        ),
    )

    worker()

    assert channel.get_fatal_error() is None
    assert channel.get_deferred_docs() == {doc.md5}
    assert worker.stop_event.is_set()
    assert db.progress[-1]["quota_deferred"] == 1
    assert db.progress[-1]["remaining"] == 1
