"""Runtime behavior for the Library metadata extraction task."""

from __future__ import annotations

import json

from app.gemini_runtime import GeminiAllKeysExhaustedError
from app.gemini_runtime import GeminiServerPauseError
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
    assert summary["processed"] == 1
    assert summary["quota_deferred"] == 1
    assert summary["remaining"] == 1
    assert repository.failures == []
    assert repository.terminal == []


def test_runtime_defers_document_with_exhausted_remaining_model_and_continues(
    monkeypatch, tmp_path
) -> None:
    base = _candidate()
    partial = MetadataExtractionCandidate(
        md5=base.md5,
        mime_type=base.mime_type,
        document_url=base.document_url,
        content_url=base.content_url,
        upstream_meta_url=base.upstream_meta_url,
        primary_storage_size=base.primary_storage_size,
        attempts=({"model": "first"}, {"model": "third"}),
    )
    next_candidate = MetadataExtractionCandidate(
        md5="b" * 32,
        mime_type=base.mime_type,
        document_url=base.document_url,
        content_url=base.content_url,
        upstream_meta_url=base.upstream_meta_url,
        primary_storage_size=base.primary_storage_size,
        attempts=(),
    )
    repository = _Repository(partial)
    repository.list_candidates = lambda **_kwargs: [partial, next_candidate]

    class PartiallyUnavailableManager(_Manager):
        def run_with_key(self, *, model_name, call, **_kwargs):  # noqa: ANN001
            if model_name == "second":
                raise GeminiAllKeysExhaustedError("second unavailable")
            return call("api-key", object())

    monkeypatch.setattr(runtime, "GeminiRuntimeManager", PartiallyUnavailableManager)
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
        models=["first", "second", "third"],
        workspace=tmp_path,
        run_id=47,
        should_stop=lambda: False,
        request_json=lambda **_kwargs: json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "Book",
                "name": "Recovered",
                "datePublished": "2001",
            }
        ),
    )

    assert summary["outcome"] == "completed"
    assert summary["processed"] == 2
    assert summary["succeeded"] == 1
    assert summary["quota_deferred"] == 1
    assert summary["remaining"] == 1
    assert repository.saved[0][0] == "b" * 32


def test_runtime_tries_next_model_after_low_quality_metadata(
    monkeypatch, tmp_path
) -> None:
    repository = _Repository(_candidate())
    monkeypatch.setattr(runtime, "GeminiRuntimeManager", _Manager)
    monkeypatch.setattr(
        runtime,
        "prepare_metadata_request",
        lambda *_args, **_kwargs: MetadataRequest(({"text": "prompt"},), {}),
    )

    def request_json(*, model_name, **_kwargs):  # noqa: ANN001
        if model_name == "first":
            return json.dumps({"@context": "https://schema.org", "@type": "Book"})
        return json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "Book",
                "name": "Recovered",
                "datePublished": "2001",
            }
        )

    summary = runtime.run_metadata_extraction(
        repository=repository,
        db=_Db(),
        storage=object(),
        primary_s3=object(),
        models=["first", "second"],
        workspace=tmp_path,
        run_id=44,
        should_stop=lambda: False,
        request_json=request_json,
    )

    assert summary["succeeded"] == 1
    assert summary["model_attempts"] == {"first": 1, "second": 1}
    assert repository.failures == [("a" * 32, "first")]
    assert repository.saved[0][2] == "second"


def test_runtime_marks_document_terminal_when_all_models_return_low_quality_metadata(
    monkeypatch, tmp_path
) -> None:
    repository = _Repository(_candidate())
    monkeypatch.setattr(runtime, "GeminiRuntimeManager", _Manager)
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
        run_id=45,
        should_stop=lambda: False,
        request_json=lambda **_kwargs: json.dumps(
            {"@context": "https://schema.org", "@type": "Book"}
        ),
    )

    assert summary["terminal"] == 1
    assert summary["succeeded"] == 0
    assert repository.failures == [
        ("a" * 32, "first"),
        ("a" * 32, "second"),
    ]
    assert repository.terminal == ["a" * 32]


def test_runtime_defers_one_document_after_repeated_service_error_and_continues(
    monkeypatch, tmp_path
) -> None:
    first = _candidate()
    second = MetadataExtractionCandidate(
        md5="b" * 32,
        mime_type=first.mime_type,
        document_url=first.document_url,
        content_url=first.content_url,
        upstream_meta_url=first.upstream_meta_url,
        primary_storage_size=first.primary_storage_size,
        attempts=(),
    )
    repository = _Repository(first)
    repository.list_candidates = lambda **_kwargs: [first, second]

    class PausedThenReadyManager(_Manager):
        calls = 0

        def run_with_key(self, *, call, **_kwargs):  # noqa: ANN001
            type(self).calls += 1
            if type(self).calls <= 2:
                raise GeminiServerPauseError("service paused")
            return call("api-key", object())

    monkeypatch.setattr(runtime, "GeminiRuntimeManager", PausedThenReadyManager)
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
        models=["first"],
        workspace=tmp_path,
        run_id=46,
        should_stop=lambda: False,
        request_json=lambda **_kwargs: json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "Book",
                "name": "Recovered",
                "datePublished": "2001",
            }
        ),
    )

    assert summary["outcome"] == "completed"
    assert summary["processed"] == 2
    assert summary["service_deferred"] == 1
    assert summary["remaining"] == 1
    assert summary["succeeded"] == 1
    assert repository.saved[0][0] == "b" * 32
