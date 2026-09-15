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
