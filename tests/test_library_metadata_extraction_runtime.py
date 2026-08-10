"""Runtime behavior for the Library metadata extraction task."""

from __future__ import annotations

import json

from app.gemini_runtime import GeminiAllKeysExhaustedError
from app.modules.library.metadata_extraction import (
    MetadataExtractionCandidate,
    MetadataRequest,
)
from app.modules.library.runtime import run_metadata_extract as runtime


class _Db:
    def __init__(self) -> None:
        self.progress: list[dict] = []
        self.events: list[tuple[str, dict]] = []

    def update_run_progress(self, _run_id, payload):  # noqa: ANN001
        self.progress.append(payload)

    def insert_event(self, event_type, **kwargs):  # noqa: ANN001
        self.events.append((event_type, kwargs))


class _Repository:
    def __init__(self, candidate: MetadataExtractionCandidate) -> None:
        self.candidate = candidate
        self.saved: list[tuple[str, dict, str]] = []
        self.failures: list[tuple[str, str]] = []
        self.terminal: list[str] = []

    def list_candidates(self, *, limit=None):  # noqa: ANN001
        return [self.candidate] if limit is None or limit > 0 else []

    def save_success(self, md5, *, schema_org, model_name):  # noqa: ANN001
        self.saved.append((md5, schema_org, model_name))
        return True

    def record_model_failure(self, md5, *, model_name, **_kwargs):  # noqa: ANN001
        self.failures.append((md5, model_name))

    def mark_terminal(self, md5, **_kwargs):  # noqa: ANN001
        self.terminal.append(md5)


class _Manager:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def run_with_key(self, *, model_name, call, **_kwargs):  # noqa: ANN001
        return call("api-key", object())


def _candidate() -> MetadataExtractionCandidate:
    return MetadataExtractionCandidate(
        md5="a" * 32,
        mime_type="application/pdf",
        document_url="https://s3.example/public/a.pdf",
        content_url=None,
        upstream_meta_url=None,
        primary_storage_size=12,
        attempts=(),
    )


def test_runtime_persists_success_and_emits_structured_progress(
    monkeypatch, tmp_path
) -> None:
    repository = _Repository(_candidate())
    db = _Db()
    monkeypatch.setattr(runtime, "GeminiRuntimeManager", _Manager)
    monkeypatch.setattr(
        runtime,
        "prepare_metadata_request",
        lambda *_args, **_kwargs: MetadataRequest(({"text": "prompt"},), {}),
    )

    summary = runtime.run_metadata_extraction(
        repository=repository,
        db=db,
        storage=object(),
        primary_s3=object(),
        models=["first", "second"],
        workspace=tmp_path,
        run_id=42,
        should_stop=lambda: False,
        request_json=lambda **_kwargs: json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "Book",
                "name": "Kitap",
                "inLanguage": "tt-Cyrl",
            }
        ),
    )

    assert summary["outcome"] == "completed"
    assert summary["succeeded"] == 1
    assert summary["model_attempts"] == {"first": 1}
    assert repository.saved[0][2] == "first"
    assert repository.saved[0][1]["name"] == "Kitap"
    assert db.events[-1][0] == "task.progress"


def test_runtime_completes_blocked_when_all_models_have_no_keys(
    monkeypatch, tmp_path
) -> None:
    class UnavailableManager(_Manager):
        def run_with_key(self, **_kwargs):  # noqa: ANN003
            raise GeminiAllKeysExhaustedError("none")

    repository = _Repository(_candidate())
    monkeypatch.setattr(runtime, "GeminiRuntimeManager", UnavailableManager)
    monkeypatch.setattr(
        runtime,
        "prepare_metadata_request",
        lambda *_args, **_kwargs: MetadataRequest(({"text": "prompt"},), {}),
    )

    summary = runtime.run_metadata_extraction(
        repository=repository,
        db=_Db(),
        storage=object(),
        primary_s3=object(),
        models=["first", "second"],
        workspace=tmp_path,
        run_id=43,
        should_stop=lambda: False,
        request_json=lambda **_kwargs: "unused",
    )

    assert summary["outcome"] == "all_keys_exhausted"
    assert summary["processed"] == 0
    assert repository.failures == []
    assert repository.terminal == []
