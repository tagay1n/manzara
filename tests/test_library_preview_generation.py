"""Library preview rendering and source-cache tests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil

import fitz
from PIL import Image, ImageDraw
import pytest

from app.modules.library.preview_detection import (
    DOCLAYNET_CONFIDENCE,
    DOCLAYNET_IMAGE_SIZE,
    DocLayNetPageDetector,
    PageAssessment,
    PreviewModelError,
    qualifying_layout_classes,
)
from app.modules.library.preview_generation import (
    PreviewGenerationSettings,
    ensure_cached_pdf,
    process_book,
    render_page_variants,
)
from app.modules.library.previews import PREVIEW_RECIPE_VERSION
from app.modules.library.runtime.run_generate_book_previews import _resolved_settings


def _make_pdf(
    path: Path,
    *,
    width: float = 300,
    height: float = 500,
    page_count: int = 1,
) -> str:
    document = fitz.open()
    for page_number in range(1, page_count + 1):
        page = document.new_page(width=width, height=height)
        page.insert_text((24, 48), f"Manzara preview test {page_number}", fontsize=18)
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
        previous = dict(self.row or {})
        same_recipe = previous.get("recipe_version") == recipe_version
        self.row = {
            "md5": md5,
            "recipe_version": recipe_version,
            "status": "processing",
            "source_page_count": previous.get("source_page_count") if same_recipe else None,
            "first_preview_page": previous.get("first_preview_page") if same_recipe else None,
            "second_preview_page": previous.get("second_preview_page") if same_recipe else None,
            "last_preview_page": previous.get("last_preview_page") if same_recipe else None,
        }
        return dict(self.row)

    def checkpoint(self, md5: str, **values) -> None:
        selected = values.get("selected_pages") or []
        selected_by_role = {page.role: page.page_number for page in selected}
        self.row = {
            "md5": md5,
            **values,
            "first_preview_page": selected_by_role.get("first"),
            "second_preview_page": selected_by_role.get("second"),
            "last_preview_page": selected_by_role.get("last"),
        }
        self.checkpoints.append(str(values["status"]))


class _PageDetector:
    def __init__(self, useful_pages: set[int]) -> None:
        self.useful_pages = set(useful_pages)
        self.calls: list[int] = []

    def assess(self, _image: Image.Image, *, page_number: int) -> PageAssessment:
        self.calls.append(page_number)
        useful = page_number in self.useful_pages
        return PageAssessment(
            useful=useful,
            detected_classes=("Text",) if useful else (),
            inference_seconds=0.25,
        )


def test_qualifying_layout_classes_rejects_header_footer_only_pages() -> None:
    assert qualifying_layout_classes(["Page-header", "Page-footer"]) == ()
    assert qualifying_layout_classes(["Page-header", "Text"]) == ("Text",)
    assert qualifying_layout_classes(["Picture", "Title", "Picture"]) == (
        "Picture",
        "Title",
    )


class _Scalar:
    def __init__(self, value: int) -> None:
        self.value = value

    def item(self) -> int:
        return self.value


class _Prediction:
    names = {0: "Page-footer", 1: "Text"}

    def __init__(self) -> None:
        self.boxes = type("Boxes", (), {"cls": [_Scalar(0), _Scalar(1)]})()

    def cpu(self):
        return self


class _Model:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[object, dict[str, object]]] = []

    def predict(self, image, **kwargs):
        self.calls.append((image, kwargs))
        if self.error:
            raise self.error
        return [_Prediction()]


def test_doclaynet_detector_uses_single_page_cpu_inference_contract() -> None:
    model = _Model()
    detector = DocLayNetPageDetector(model)
    image = Image.new("RGB", (300, 500), "white")

    assessment = detector.assess(image, page_number=4)

    assert assessment.useful is True
    assert assessment.detected_classes == ("Text",)
    assert len(model.calls) == 1
    assert model.calls[0][0] is image
    assert model.calls[0][1] == {
        "verbose": False,
        "imgsz": DOCLAYNET_IMAGE_SIZE,
        "device": "cpu",
        "conf": DOCLAYNET_CONFIDENCE,
        "iou": 0.45,
        "max_det": 300,
        "agnostic_nms": False,
    }


def test_doclaynet_detector_surfaces_inference_as_fatal_model_error() -> None:
    detector = DocLayNetPageDetector(_Model(error=RuntimeError("operator unavailable")))

    with pytest.raises(PreviewModelError, match="page 2.*operator unavailable"):
        detector.assess(Image.new("RGB", (20, 20)), page_number=2)


@pytest.mark.skipif(
    os.environ.get("MANZARA_RUN_PREVIEW_MODEL_SMOKE") != "1",
    reason="requires the pinned DocLayNet weight and CPU inference dependencies",
)
def test_doclaynet_cached_model_smoke() -> None:
    artifacts_root = Path(
        os.environ.get("MANZARA_ARTIFACTS_ROOT", "~/.manzara")
    ).expanduser()
    detector = DocLayNetPageDetector.from_huggingface(
        cache_dir=artifacts_root / "models" / "huggingface"
    )
    white = Image.new("RGB", (768, 1024), "white")
    title = Image.new("RGB", (768, 1024), "white")
    ImageDraw.Draw(title).text(
        (240, 180),
        "BOOK TITLE",
        fill="black",
        font_size=48,
    )

    assert detector.assess(white, page_number=1).useful is False
    assert detector.assess(title, page_number=2).useful is True


def test_process_book_uploads_only_expected_short_document_objects_and_resumes(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    digest = _make_pdf(source)
    source_s3 = _DownloadS3(source)
    target_s3 = _PreviewS3(source)
    repository = _PreviewRepository()
    settings = PreviewGenerationSettings(
        source_bucket="ttdoc",
        target_bucket="ttbook-previews",
        cache_dir=tmp_path / "cache",
        workspace=tmp_path / "workspace",
    )
    detector = _PageDetector({1})

    first = process_book(
        {"md5": digest},
        repository=repository,
        settings=settings,
        source_s3=source_s3,
        target_s3=target_s3,
        page_detector=detector,
        run_id=7,
        log=lambda _message: None,
    )
    second = process_book(
        {"md5": digest},
        repository=repository,
        settings=settings,
        source_s3=source_s3,
        target_s3=target_s3,
        page_detector=detector,
        run_id=8,
        log=lambda _message: None,
    )

    assert first.status == "ready"
    assert first.uploaded_objects == 2
    assert second.status == "ready"
    assert second.reused_objects == 2
    assert source_s3.calls == [("ttdoc", f"{digest}.pdf")]
    assert detector.calls == [1]
    assert len(target_s3.upload_calls) == 2
    assert {key.rsplit("/", 1)[-1] for key in target_s3.objects} == {
        "1s.webp",
        "1l.webp",
    }
    assert set(target_s3.objects) == {f"{digest}/1s.webp", f"{digest}/1l.webp"}
    assert repository.row is not None
    assert repository.row["recipe_version"] == PREVIEW_RECIPE_VERSION
    assert repository.checkpoints[-1] == "ready"
    assert (settings.workspace / digest / "1s.webp").is_file()
    assert (settings.workspace / digest / "1l.webp").is_file()


def test_process_book_replaces_blank_targets_with_detected_edge_pages(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    digest = _make_pdf(source, page_count=8)
    source_s3 = _DownloadS3(source)
    target_s3 = _PreviewS3(source)
    repository = _PreviewRepository()
    detector = _PageDetector({1, 3, 7})
    settings = PreviewGenerationSettings(
        source_bucket="ttdoc",
        target_bucket="ttbook-previews",
        cache_dir=tmp_path / "cache",
        workspace=tmp_path / "workspace",
    )

    result = process_book(
        {"md5": digest},
        repository=repository,
        settings=settings,
        source_s3=source_s3,
        target_s3=target_s3,
        page_detector=detector,
        run_id=9,
        log=lambda _message: None,
    )

    assert result.status == "ready"
    assert result.selected_pages == 3
    assert result.rejected_pages == 2
    assert detector.calls == [1, 8, 7, 2, 3]
    assert len(detector.calls) == len(set(detector.calls))
    assert repository.row is not None
    selected = repository.row["selected_pages"]
    assert [(page.role, page.page_number) for page in selected] == [
        ("first", 1),
        ("second", 3),
        ("last", 7),
    ]
    metadata_by_alias = {
        key.rsplit("/", 1)[-1]: value["Metadata"]
        for key, value in target_s3.objects.items()
    }
    assert metadata_by_alias["1s.webp"]["page-number"] == "1"
    assert metadata_by_alias["2s.webp"]["page-number"] == "3"
    assert metadata_by_alias["ls.webp"]["page-number"] == "7"


def test_process_book_completes_empty_selection_without_uploads(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    digest = _make_pdf(source, page_count=10)
    repository = _PreviewRepository()
    target_s3 = _PreviewS3(source)
    detector = _PageDetector(set())

    result = process_book(
        {"md5": digest},
        repository=repository,
        settings=PreviewGenerationSettings(
            source_bucket="ttdoc",
            target_bucket="ttbook-previews",
            cache_dir=tmp_path / "cache",
            workspace=tmp_path / "workspace",
        ),
        source_s3=_DownloadS3(source),
        target_s3=target_s3,
        page_detector=detector,
        run_id=10,
        log=lambda _message: None,
    )

    assert result.status == "ready"
    assert result.selected_pages == 0
    assert result.rejected_pages == 6
    assert detector.calls == [1, 2, 3, 10, 9, 8]
    assert target_s3.upload_calls == []
    assert repository.row is not None
    assert repository.row["selected_pages"] == []
    assert repository.checkpoints[-1] == "ready"


def test_preview_settings_use_backblaze_for_source_and_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MANZARA_ARTIFACTS_ROOT", str(tmp_path / "artifacts"))
    payload = {
        "documents": {
            "cache_path": str(tmp_path / "cache"),
            "primary_storage": {
                "endpoint_url": "https://s3.eu-central-003.backblazeb2.com",
                "region_name": "eu-central-003",
                "access_key_id": "b2-id",
                "secret_access_key": "b2-secret",
                "bucket": {
                    "public": "b2-docs",
                    "private": "b2-private",
                    "book_previews": "ttpreviews",
                },
            },
        },
        "yandex": {
            "disk": {
                "oauth_token": "disk-token",
                "documents": {
                    "source_path": "/documents",
                    "restricted_path": "/documents/private",
                    "filtered_out_path": "/documents/filtered-out",
                },
            },
            "cloud": {
                "endpoint_url": "https://storage.yandexcloud.net",
                "region_name": "ru-central1",
                "aws_access_key_id": "yc-id",
                "aws_secret_access_key": "yc-secret",
                "bucket": {
                    "document": "legacy-docs",
                    "document_private": "legacy-private",
                    "upstream_metadata": "upstream",
                },
            },
        },
        "encryption_key": "encryption-key",
    }

    settings, credentials = _resolved_settings(payload, run_id=77)

    assert settings.source_bucket == "b2-docs"
    assert settings.source_endpoint_url == (
        "https://s3.eu-central-003.backblazeb2.com"
    )
    assert settings.target_bucket == "ttpreviews"
    assert credentials["source_access_key_id"] == "b2-id"
    assert credentials["target_access_key_id"] == "b2-id"
    assert credentials["target_endpoint_url"] == (
        "https://s3.eu-central-003.backblazeb2.com"
    )
    assert settings.model_cache_dir == tmp_path / "artifacts" / "models" / "huggingface"
