"""Library non pdf runtime coverage."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.modules.library.corrupt_document import CorruptDocumentError
from app.modules.library.non_pdf_repository import NonPdfCandidate
from app.modules.library.runtime.run_extract_non_pdf import (
    _delete_stale_assets,
    _failure_status,
    _write_content_archive,
    run_extraction,
)


def test_content_archive_contains_exact_md5_markdown_member(tmp_path: Path) -> None:
    md5 = "a" * 32
    archive_path = _write_content_archive(md5, "# Content\n", tmp_path / f"{md5}.zip")

    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == [f"{md5}.md"]
        assert archive.read(f"{md5}.md") == b"# Content\n"
        assert archive.getinfo(f"{md5}.md").date_time == (1980, 1, 1, 0, 0, 0)


class _AssetS3:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def list_objects_v2(self, **_request):
        return {
            "Contents": [
                {"Key": "a" * 32 + "/1.png"},
                {"Key": "a" * 32 + "/2.png"},
                {"Key": "a" * 32 + "/old.png"},
            ],
            "IsTruncated": False,
        }

    def delete_object(self, *, Bucket, Key):  # noqa: ANN001, N803
        del Bucket
        self.deleted.append(Key)


def test_stale_image_cleanup_keeps_only_current_manifest() -> None:
    md5 = "a" * 32
    s3 = _AssetS3()

    deleted = _delete_stale_assets(
        s3,
        bucket="images",
        md5=md5,
        expected_keys={f"{md5}/1.png", f"{md5}/2.png"},
    )

    assert deleted == 1
    assert s3.deleted == [f"{md5}/old.png"]


class _CorruptRuntimeRepository:
    def __init__(self, candidate: NonPdfCandidate) -> None:
        self.candidate = candidate
        self.outcomes: list[dict] = []

    def list_candidates(self, **_kwargs):
        return [self.candidate]

    def start_attempt(self, *_args, **_kwargs):
        return None

    def mark_outcome(self, _md5, **kwargs):  # noqa: ANN001
        self.outcomes.append(dict(kwargs))


class _CorruptCleanupRepository:
    def __init__(self) -> None:
        self.plans: list[dict] = []

    def enqueue_cleanup(self, payload):  # noqa: ANN001
        self.plans.append(dict(payload))
        return len(self.plans), True


class _ProgressDb:
    def publish_run_progress(self, **_kwargs):
        return None


def test_non_pdf_runtime_queues_structural_corruption(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.docx"
    source.write_bytes(b"broken")
    candidate = NonPdfCandidate(
        md5="a" * 32,
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        source_path="/documents/nested/source.docx",
        document_url="https://s3.example/source.docx",
        primary_storage_size=6,
        content_url=None,
    )
    repository = _CorruptRuntimeRepository(candidate)
    cleanup = _CorruptCleanupRepository()
    storage = type(
        "Storage",
        (),
        {
            "cache_path": tmp_path / "cache",
            "source_path": "/documents",
            "filtered_out_path": "/filtered",
            "content_bucket": "content",
            "content_images_bucket": "images",
        },
    )()
    monkeypatch.setattr(
        "app.modules.library.runtime.run_extract_non_pdf.download_cached_primary_document",
        lambda **_kwargs: source,
    )
    monkeypatch.setattr(
        "app.modules.library.runtime.run_extract_non_pdf.prepare_extraction",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            CorruptDocumentError("document_container", "invalid ZIP")
        ),
    )

    summary = run_extraction(
        repository=repository,
        cleanup_repository=cleanup,
        db=_ProgressDb(),
        s3=object(),
        storage=storage,
        workspace=tmp_path / "run",
        run_id=71,
        should_stop=lambda: False,
    )

    assert summary["corrupted"] == 1
    assert cleanup.plans[0]["target_path"] == ("/filtered/corrupted/nested/source.docx")
    assert cleanup.plans[0]["evidence"]["detector"] == "document_container"
    assert repository.outcomes[0]["status"] == "failed"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            ValueError("Extracted document contains only images; OCR required"),
            "deferred",
        ),
        (ValueError("Rendered Markdown validation failed: bad image"), "deferred"),
        (RuntimeError("LibreOffice produced 0 DOCX files"), "deferred"),
        (
            RuntimeError(
                "pandoc-read failed: couldn't unpack docx container: "
                "Content size mismatch"
            ),
            "deferred",
        ),
        (RuntimeError("libreoffice timed out after 900 seconds"), "failed"),
        (RuntimeError("temporary S3 failure"), "failed"),
    ],
)
def test_failure_status_separates_deterministic_and_retryable_errors(
    error: Exception, expected: str
) -> None:
    assert _failure_status(error) == expected


def test_pptx_deferral_preserves_content_and_emits_inspection(monkeypatch, tmp_path):
    import json
    from app.modules.library.non_pdf_types import DeferredDocumentExtraction

    candidate = NonPdfCandidate(
        md5="a" * 32,
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        source_path="/deck.pptx",
        document_url="https://example/deck.pptx",
        primary_storage_size=10,
        content_url="https://example/existing.zip",
    )
    repository = _CorruptRuntimeRepository(candidate)
    cleanup = _CorruptCleanupRepository()
    events = []
    db = _ProgressDb()
    db.insert_event = lambda **kwargs: events.append(kwargs)
    storage = type(
        "Storage",
        (),
        {
            "cache_path": tmp_path / "cache",
            "content_bucket": "content",
            "content_images_bucket": "images",
        },
    )()
    source = tmp_path / "source.pptx"
    source.write_bytes(b"placeholder")
    monkeypatch.setattr(
        "app.modules.library.runtime.run_extract_non_pdf.download_cached_primary_document",
        lambda **kwargs: source,
    )

    def deferred(source, *, workspace, **kwargs):
        report = {
            "kind": "library.pptx_inspection",
            "inspection_complete": True,
            "slide_count": 2,
            "visible_slide_count": 2,
            "image_slide_count": 1,
            "unsupported_visual_slide_count": 1,
            "reasons": ["pptx_slide_images", "pptx_unsupported_visuals"],
            "slides": [],
        }
        (workspace / "pptx-inspection.json").write_text(json.dumps(report))
        raise DeferredDocumentExtraction("pptx", "pptx_slide_images")

    monkeypatch.setattr(
        "app.modules.library.runtime.run_extract_non_pdf.prepare_extraction", deferred
    )
    summary = run_extraction(
        repository=repository,
        cleanup_repository=cleanup,
        db=db,
        s3=object(),
        storage=storage,
        workspace=tmp_path / "run",
        run_id=71,
        should_stop=lambda: False,
    )
    assert summary["deferred"] == 1
    assert summary["pptx_inspected"] == 1
    assert summary["pptx_image_decks"] == summary["pptx_unsupported_visual_decks"] == 1
    assert summary["pptx_extracted"] == 0
    assert repository.outcomes[0]["status"] == "deferred"
    assert repository.outcomes[0]["detected_format"] == "pptx"
    assert repository.outcomes[0]["error_text"].startswith("pptx_slide_images:")
    assert candidate.content_url == "https://example/existing.zip"
    assert not cleanup.plans
    assert events[0]["event_type"] == "task.artifact"
    assert events[0]["payload"]["md5"] == candidate.md5
    assert "slides" not in events[0]["payload"]


def test_pptx_counters_survive_compact_artifact_projection():
    from app.task_runtime.logging import TaskLoggingMixin

    payload = TaskLoggingMixin()._artifact_event_payload(
        {
            "kind": "library.non_pdf_extraction_summary",
            "pptx_inspected": 7,
            "pptx_extracted": 2,
            "pptx_image_decks": 4,
            "pptx_unsupported_visual_decks": 2,
            "pptx_empty_decks": 1,
        }
    )
    assert payload["pptx_inspected"] == 7
    assert payload["pptx_image_decks"] == 4


def test_local_markdown_and_archive_survive_publication_failure(monkeypatch, tmp_path):
    from app.modules.library.non_pdf_types import PreparedExtraction

    candidate = NonPdfCandidate(
        md5="a" * 32,
        mime_type="text/plain",
        source_path="/book.txt",
        document_url="https://example/book.txt",
        primary_storage_size=4,
        content_url=None,
    )
    repository = _CorruptRuntimeRepository(candidate)
    db = _ProgressDb()
    events = []
    db.insert_event = lambda **kwargs: events.append(kwargs)
    storage = type(
        "Storage",
        (),
        {
            "cache_path": tmp_path / "cache",
            "content_bucket": "content",
            "content_images_bucket": "images",
        },
    )()
    source = tmp_path / "source.txt"
    source.write_text("Text")
    monkeypatch.setattr(
        "app.modules.library.runtime.run_extract_non_pdf.download_cached_primary_document",
        lambda **kwargs: source,
    )
    monkeypatch.setattr(
        "app.modules.library.runtime.run_extract_non_pdf.prepare_extraction",
        lambda source, workspace, **kwargs: PreparedExtraction(
            "text", workspace, None, "Text — “quote”\n", ()
        ),
    )
    monkeypatch.setattr(
        "app.modules.library.runtime.run_extract_non_pdf._matching_object",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("Storage unavailable")
        ),
    )
    summary = run_extraction(
        repository=repository,
        db=db,
        s3=object(),
        storage=storage,
        workspace=tmp_path / "run",
        run_id=71,
        should_stop=lambda: False,
    )
    doc = tmp_path / "run" / candidate.md5
    markdown = (doc / "final.md").read_text()
    assert (doc / "unformatted.md").is_file()
    assert (doc / "validation.json").is_file()
    with zipfile.ZipFile(doc / (candidate.md5 + ".zip")) as archive:
        assert archive.read(candidate.md5 + ".md").decode() == markdown
    assert summary["failed"] == 1
    assert summary["workspace_path"] == str(tmp_path / "run")
    content_event = next(
        e for e in events if e["payload"]["kind"] == "library.non_pdf_local_content"
    )
    assert content_event["event_type"] == "task.artifact"
    assert content_event["payload"]["markdown_path"] == str(doc / "final.md")
    assert content_event["payload"]["archive_path"] == str(
        doc / (candidate.md5 + ".zip")
    )


def test_non_pdf_artifact_keeps_local_workspace_link():
    from app.task_runtime.logging import TaskLoggingMixin

    payload = TaskLoggingMixin()._artifact_event_payload(
        {
            "kind": "library.non_pdf_extraction_summary",
            "workspace_path": "/tmp/review/run-71",
        }
    )
    assert payload["workspace_path"] == "/tmp/review/run-71"
