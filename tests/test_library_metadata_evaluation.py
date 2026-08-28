"""Metadata evaluation quality and resumability policy tests."""

from __future__ import annotations

import inspect
import hashlib
import json
from queue import Queue
from types import SimpleNamespace

import pytest

from app.gemini_model_pool import GeminiModelResponseError
from app.gemini_model_pool import GeminiModelPoolOperationalError
from app.gemini_model_pool import GeminiModelPoolResult
from app.gemini_model_pool import GeminiModelPoolUnavailableError
from app.modules.library.runtime.run_meta_evaluate import _bootstrap_import_paths


_bootstrap_import_paths()

from metadata.evaluation import (  # noqa: E402
    Channel,
    Evaluation,
    EvaluationTask,
    LibraryApplicabilityWorker,
    _parse_evaluation_response,
)
from metadata import evaluation as evaluation_module  # noqa: E402
from metadata import evaluation_helpers as evaluation_helpers_module  # noqa: E402
from metadata.repository import fetch_docs_for_evaluation  # noqa: E402


def _document() -> EvaluationTask:
    return EvaluationTask(
        md5="a" * 32,
        ya_path="/books/a.pdf",
        language="tt-Cyrl",
        page_count=10,
        full=True,
        sharing_restricted=False,
        ya_public_url=None,
        mime_type="application/pdf",
        document_url="https://example.test/a.pdf",
        upstream_meta_url=None,
        content_url=None,
        schema_org={
            "@context": "https://schema.org",
            "@type": "Book",
            "name": "A",
        },
    )


def test_evaluation_response_requires_classification_only_when_applicable() -> None:
    with pytest.raises(GeminiModelResponseError, match="classification"):
        _parse_evaluation_response(
            json.dumps({"applicable": True, "reason": "Tatar literary work"}),
            doc=_document(),
            config={"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}},
        )

    result = _parse_evaluation_response(
        json.dumps({"applicable": False, "reason": "not a library document"}),
        doc=_document(),
        config={"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}},
    )

    assert result.applicable is False
    assert result.reason == "not a library document"

    with pytest.raises(GeminiModelResponseError, match="reason"):
        _parse_evaluation_response(
            json.dumps({"applicable": False}),
            doc=_document(),
            config={"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}},
        )


def test_evaluation_selection_reopens_only_incomplete_or_inconsistent_rows() -> None:
    source = inspect.getsource(fetch_docs_for_evaluation)

    assert "Metadata.lib.is_(None)" in source
    assert "Metadata.classification_id.is_(None)" in source
    assert "Metadata.lib_eval_method" not in source
    assert "LibraryMetadataEvaluationState" in source
    assert "model_pool" in inspect.signature(fetch_docs_for_evaluation).parameters


def test_evaluation_replaces_invalid_pdf_in_configured_shared_cache(
    monkeypatch, tmp_path
) -> None:
    content = b"verified-evaluation-pdf"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    cached = tmp_path / f"{digest}.pdf"
    cached.write_bytes(b"corrupt")
    doc = _document()
    doc.md5 = digest
    downloads: list[str] = []

    monkeypatch.setattr(
        evaluation_helpers_module,
        "load_document_storage_settings",
        lambda _config: SimpleNamespace(cache_path=tmp_path),
    )
    monkeypatch.setattr(
        evaluation_helpers_module,
        "_resolve_doc_source_url",
        lambda *_args: "https://example.test/document.pdf",
    )

    def download(_url, local_path):  # noqa: ANN001
        downloads.append(str(local_path))
        evaluation_helpers_module.Path(local_path).write_bytes(content)

    monkeypatch.setattr(evaluation_helpers_module, "_download_file", download)

    result = evaluation_helpers_module._ensure_pdf_in_shared_cache(doc, {}, object())

    assert result == str(cached)
    assert cached.read_bytes() == content
    assert len(downloads) == 1


def test_worker_advances_to_next_model_after_incomplete_response_and_logs_attempts(
    monkeypatch,
    capsys,
) -> None:
    class _Manager:
        def run_with_key(self, *, model_name, call, run_id, max_attempts):  # noqa: ANN001
            assert max_attempts == 1
            return call("test-key", object())

    calls: list[str] = []

    def _request(**kwargs):  # noqa: ANN003
        model = kwargs["model_name"]
        calls.append(model)
        if model == "model-first":
            return json.dumps({"applicable": True})
        return json.dumps(
            {
                "applicable": True,
                "reason": "Tatar literary work",
                "library_ddc": "894.36",
                "library_path": ["Literature", "Tatar literature"],
            }
        )

    monkeypatch.setattr(evaluation_module, "generate_structured_json", _request)
    monkeypatch.setattr(
        evaluation_module,
        "get_evaluation_attempted_models",
        lambda _md5: set(),
    )
    worker = LibraryApplicabilityWorker(
        tasks_queue=Queue(),
        config={"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}},
        channel=Channel(dry_run=True),
        dry_run=True,
        excerpt_chars=0,
        gemini_manager=_Manager(),
        models=["model-first", "model-second"],
    )
    monkeypatch.setattr(worker, "_prepare_pdf_slice_for_eval", lambda _doc: None)
    monkeypatch.setattr(worker, "_dump_prompt", lambda _md5, _prompt: None)

    result = worker._evaluate(_document())

    assert result.model_name == "model-second"
    assert calls == ["model-first", "model-second"]
    output = capsys.readouterr().out
    assert "Gemini request" in output
    assert f"md5={_document().md5}" in output
    assert "model=model-first" in output
    assert "model=model-second" in output


def test_evaluation_progress_matches_metadata_extraction_contract() -> None:
    class _Db:
        def __init__(self) -> None:
            self.progress: list[dict] = []
            self.events: list[tuple[str, dict]] = []

        def update_run_progress(self, _run_id, payload):  # noqa: ANN001
            self.progress.append(payload)

        def insert_event(self, event_type, **kwargs):  # noqa: ANN001
            self.events.append((event_type, kwargs))

    db = _Db()
    progress = evaluation_module._EvaluationProgress(db, run_id=42, total=2)

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


def test_worker_defers_retryable_service_error_and_continues(monkeypatch) -> None:
    class _Db:
        def __init__(self) -> None:
            self.progress: list[dict] = []

        def update_run_progress(self, _run_id, payload):  # noqa: ANN001
            self.progress.append(payload)

        def insert_event(self, _event_type, **_kwargs):  # noqa: ANN001
            pass

    first = _document()
    second = _document()
    second.md5 = "b" * 32
    tasks = Queue()
    tasks.put(first)
    tasks.put(second)
    channel = Channel(dry_run=False)
    db = _Db()
    progress = evaluation_module._EvaluationProgress(db, run_id=42, total=2)
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

    def evaluate(doc):  # noqa: ANN001
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

    monkeypatch.setattr(worker, "_evaluate", evaluate)
    monkeypatch.setattr(
        worker,
        "_save_result",
        lambda md5, _evaluation, *, model_name: saved.append(md5),
    )

    worker()

    assert saved == [second.md5]
    assert channel.get_fatal_error() is None
    assert channel.get_deferred_docs() == {first.md5}
    assert db.progress[-1]["current"] == 2
    assert db.progress[-1]["succeeded"] == 1
    assert db.progress[-1]["service_deferred"] == 1
    assert db.progress[-1]["remaining"] == 1


def test_worker_stops_cleanly_when_all_models_are_quota_unavailable(
    monkeypatch,
) -> None:
    class _Db:
        def __init__(self) -> None:
            self.progress: list[dict] = []

        def update_run_progress(self, _run_id, payload):  # noqa: ANN001
            self.progress.append(payload)

        def insert_event(self, _event_type, **_kwargs):  # noqa: ANN001
            pass

    doc = _document()
    tasks = Queue()
    tasks.put(doc)
    channel = Channel(dry_run=False)
    db = _Db()
    progress = evaluation_module._EvaluationProgress(db, run_id=42, total=1)
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
        worker,
        "_evaluate",
        lambda _doc: (_ for _ in ()).throw(
            GeminiModelPoolUnavailableError(["model-first", "model-second"])
        ),
    )

    worker()

    assert channel.get_fatal_error() is None
    assert channel.get_deferred_docs() == {doc.md5}
    assert worker.stop_event.is_set()
    assert db.progress[-1]["quota_deferred"] == 1
    assert db.progress[-1]["remaining"] == 1
