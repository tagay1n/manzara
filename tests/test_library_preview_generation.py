"""Library preview rendering and source-cache tests."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil

import fitz
from PIL import Image

from app.modules.library.preview_generation import (
    PreviewGenerationSettings,
    ensure_cached_pdf,
    process_book,
    render_page_variants,
)
from app.modules.library.previews import PREVIEW_RECIPE_VERSION


def _make_pdf(path: Path, *, width: float = 300, height: float = 500) -> str:
    document = fitz.open()
    page = document.new_page(width=width, height=height)
    page.insert_text((24, 48), "Manzara preview test", fontsize=18)
    document.save(path)
    document.close()
    return hashlib.md5(path.read_bytes()).hexdigest()  # noqa: S324 - content identity contract


def test_render_page_variants_preserves_aspect_ratio_and_bounds(tmp_path: Path) -> None:
    pdf_path = tmp_path / "source.pdf"
    _make_pdf(pdf_path, width=300, height=500)

    variants = render_page_variants(
        pdf_path,
        page_number=1,
        object_alias="1",
        output_dir=tmp_path / "rendered",
    )

    assert set(variants) == {"small", "large"}
    assert variants["small"].path.name == "1s.webp"
    assert variants["large"].path.name == "1l.webp"
    assert variants["small"].width <= 400
    assert variants["small"].height <= 600
    assert variants["large"].width <= 1000
    assert variants["large"].height <= 1500
    assert abs(
        (variants["large"].width / variants["large"].height) - (300 / 500)
    ) < 0.01
    with Image.open(variants["small"].path) as image:
        assert image.format == "WEBP"
        assert image.size == (variants["small"].width, variants["small"].height)


class _DownloadS3:
    def __init__(self, source: Path) -> None:
        self.source = source
        self.calls: list[tuple[str, str]] = []

    def download_file(self, bucket: str, key: str, target: str) -> None:
        self.calls.append((bucket, key))
        shutil.copyfile(self.source, target)


def test_ensure_cached_pdf_downloads_atomically_and_reuses_verified_file(tmp_path: Path) -> None:
    source = tmp_path / "remote.pdf"
    digest = _make_pdf(source)
    cache_dir = tmp_path / "cache"
    s3 = _DownloadS3(source)

    first_path, first_downloaded = ensure_cached_pdf(
        digest,
        cache_dir=cache_dir,
        source_bucket="ttdoc",
        s3=s3,
    )
    second_path, second_downloaded = ensure_cached_pdf(
        digest,
        cache_dir=cache_dir,
        source_bucket="ttdoc",
        s3=s3,
    )

    assert first_path == cache_dir / f"{digest}.pdf"
    assert second_path == first_path
    assert first_downloaded is True
    assert second_downloaded is False
    assert s3.calls == [("ttdoc", f"{digest}.pdf")]
    assert list(cache_dir.glob("*.part")) == []


def test_ensure_cached_pdf_rejects_download_with_wrong_md5(tmp_path: Path) -> None:
    source = tmp_path / "remote.pdf"
    _make_pdf(source)
    expected = "a" * 32
    s3 = _DownloadS3(source)

    try:
        ensure_cached_pdf(
            expected,
            cache_dir=tmp_path / "cache",
            source_bucket="ttdoc",
            s3=s3,
        )
    except ValueError as exc:
        assert "MD5 mismatch" in str(exc)
    else:
        raise AssertionError("Expected source hash validation failure")

    assert not (tmp_path / "cache" / f"{expected}.pdf").exists()
    assert list((tmp_path / "cache").glob("*.part")) == []


class _PreviewS3(_DownloadS3):
    def __init__(self, source: Path) -> None:
        super().__init__(source)
        self.objects: dict[str, dict[str, object]] = {}
        self.upload_calls: list[str] = []

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        _ = Bucket
        if Key not in self.objects:
            error = RuntimeError("not found")
            error.response = {"Error": {"Code": "404"}}  # type: ignore[attr-defined]
            raise error
        return self.objects[Key]

    def upload_file(self, path: str, bucket: str, key: str, *, ExtraArgs: dict) -> None:
        _ = bucket
        self.upload_calls.append(key)
        self.objects[key] = {
            "ContentLength": Path(path).stat().st_size,
            "ETag": '"test-etag"',
            "Metadata": dict(ExtraArgs["Metadata"]),
        }


class _PreviewRepository:
    def __init__(self) -> None:
        self.row: dict[str, object] | None = None
        self.checkpoints: list[str] = []

    def start_attempt(self, md5: str, *, recipe_version: str, run_id: int | None):
        _ = run_id
        manifest = dict(self.row.get("manifest", {})) if self.row else {}
        self.row = {
            "md5": md5,
            "recipe_version": recipe_version,
            "status": "processing",
            "manifest": manifest,
        }
        return dict(self.row)

    def checkpoint(self, md5: str, **values) -> None:
        self.row = {"md5": md5, **values}
        self.checkpoints.append(str(values["status"]))


def test_process_book_uploads_only_expected_short_document_objects_and_resumes(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    digest = _make_pdf(source)
    s3 = _PreviewS3(source)
    repository = _PreviewRepository()
    settings = PreviewGenerationSettings(
        source_bucket="ttdoc",
        target_bucket="ttbook-previews",
        cache_dir=tmp_path / "cache",
        workspace=tmp_path / "workspace",
    )

    first = process_book(
        {"md5": digest},
        repository=repository,
        settings=settings,
        s3=s3,
        run_id=7,
        log=lambda _message: None,
    )
    second = process_book(
        {"md5": digest},
        repository=repository,
        settings=settings,
        s3=s3,
        run_id=8,
        log=lambda _message: None,
    )

    assert first.status == "ready"
    assert first.uploaded_objects == 2
    assert second.status == "ready"
    assert second.reused_objects == 2
    assert len(s3.upload_calls) == 2
    assert {key.rsplit("/", 1)[-1] for key in s3.objects} == {"1s.webp", "1l.webp"}
    assert repository.row is not None
    assert repository.row["recipe_version"] == PREVIEW_RECIPE_VERSION
    assert repository.checkpoints[-1] == "ready"
