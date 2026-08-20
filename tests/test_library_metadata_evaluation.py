"""Metadata evaluation quality and resumability policy tests."""

from __future__ import annotations

import inspect
import hashlib
import json
from queue import Queue
from types import SimpleNamespace

import pytest

from app.gemini_model_pool import GeminiModelResponseError
from app.modules.library.runtime.run_meta_evaluate import _bootstrap_import_paths


_bootstrap_import_paths()

from metadata.evaluation import (  # noqa: E402
    Channel,
    EvaluationTask,
    LibraryApplicabilityWorker,
    _parse_evaluation_response,
)
from metadata import evaluation as evaluation_module  # noqa: E402
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
        schema_org={"name": "A"},
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
        evaluation_module,
        "load_document_storage_settings",
        lambda _config: SimpleNamespace(cache_path=tmp_path),
    )
    monkeypatch.setattr(
        evaluation_module,
        "_resolve_doc_source_url",
        lambda *_args: "https://example.test/document.pdf",
    )

    def download(_url, local_path):  # noqa: ANN001
        downloads.append(str(local_path))
        evaluation_module.Path(local_path).write_bytes(content)

    monkeypatch.setattr(evaluation_module, "_download_file", download)

    result = evaluation_module._ensure_pdf_in_shared_cache(doc, {}, object())

    assert result == str(cached)
    assert cached.read_bytes() == content
    assert len(downloads) == 1


def test_worker_advances_to_next_model_after_incomplete_response(
    monkeypatch,
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
