"""Rendering and storage primitives for Library PDF previews."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import fitz
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from PIL import Image

from app.document_storage import (
    find_valid_cache_file,
    materialize_cached_document,
)
from app.modules.library.preview_detection import PageAssessment
from app.modules.library.previews import (
    PREVIEW_RECIPE_VERSION,
    PreviewPage,
    preview_object_key,
    select_informative_preview_pages,
)
from app.runtime_config import config_integer, config_text
from app.s3_transfer import sequential_transfer_config


@dataclass(frozen=True)
class RenderedVariant:
    """One rendered WebP file and its public dimensions."""

    path: Path
    width: int
    height: int
    quality: int


@dataclass(frozen=True)
class PreviewGenerationSettings:
    """Resolved source, target, and local workspace settings."""

    target_bucket: str
    cache_dir: Path
    workspace: Path
    model_cache_dir: Path
    cache_max_bytes: int


@dataclass(frozen=True)
class BookPreviewResult:
    """Structured outcome for one candidate document."""

    md5: str
    source_page_count: int
    pages: tuple[dict[str, Any], ...]
    uploaded_objects: int = 0
    reused_objects: int = 0
    downloaded_source: bool = False
    inspected_pages: int = 0
    rejected_pages: int = 0
    selected_pages: int = 0
    inference_seconds: float = 0.0


def _target_size(width: int, height: int, max_width: int, max_height: int) -> tuple[int, int]:
    scale = min(float(max_width) / float(width), float(max_height) / float(height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def _render_page_image(document: fitz.Document, page_number: int) -> Image.Image:
    index = int(page_number) - 1
    if index < 0 or index >= document.page_count:
        raise ValueError(f"PDF page {page_number} is outside 1..{document.page_count}")
    page = document.load_page(index)
    rect = page.rect
    large_width, large_height = _target_size(
        max(1, round(rect.width)),
        max(1, round(rect.height)),
        config_integer("previews", "large", "max_width"),
        config_integer("previews", "large", "max_height"),
    )
    scale = min(large_width / rect.width, large_height / rect.height)
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale),
        alpha=False,
        colorspace=fitz.csRGB,
    )
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def render_page_variants(
    pdf_path: Path,
    *,
    page_number: int,
    object_alias: str,
    output_dir: Path,
) -> dict[str, RenderedVariant]:
    """Render one 1-based PDF page into the versioned small/large recipe."""
    output_dir.mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf_path) as document:
        large_image = _render_page_image(document, page_number)

    small_size = _target_size(large_image.width, large_image.height, config_integer("previews", "small", "max_width"), config_integer("previews", "small", "max_height"))
    small_image = large_image.resize(small_size, Image.Resampling.LANCZOS)
    small_path = output_dir / f"{object_alias}s.webp"
    large_path = output_dir / f"{object_alias}l.webp"
    small_image.save(small_path, format="WEBP", quality=config_integer("previews", "small", "quality", maximum=100), method=config_integer("previews", "webp_method", minimum=0, maximum=6))
    large_image.save(large_path, format="WEBP", quality=config_integer("previews", "large", "quality", maximum=100), method=config_integer("previews", "webp_method", minimum=0, maximum=6))
    return {
        "small": RenderedVariant(
            path=small_path,
            width=small_image.width,
            height=small_image.height,
            quality=config_integer("previews", "small", "quality", maximum=100),
        ),
        "large": RenderedVariant(
            path=large_path,
            width=large_image.width,
            height=large_image.height,
            quality=config_integer("previews", "large", "quality", maximum=100),
        ),
    }


def ensure_cached_pdf(
    md5: str,
    *,
    cache_dir: Path,
    source_bucket: str,
    source_key: str | None = None,
    s3: Any,
    cache_max_bytes: int,
) -> tuple[Path, bool]:
    """Return a hash-verified cached PDF, atomically downloading when absent."""
    digest = str(md5 or "").strip().lower()
    cached_before = find_valid_cache_file(cache_dir, digest)
    if cached_before is not None:
        return cached_before, False

    def download(temporary: Path) -> None:
        s3.download_file(
            str(source_bucket), str(source_key or f"{digest}.pdf"), str(temporary),
            Config=sequential_transfer_config(),
        )

    target = materialize_cached_document(
        cache_path=cache_dir,
        expected_md5=digest,
        extension=".pdf",
        download=download,
        cache_max_bytes=cache_max_bytes,
    )
    return target, True


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            return str(error.get("Code") or "").strip()
    return ""


def _is_storage_fatal(exc: Exception) -> bool:
    if isinstance(
        exc,
        (EndpointConnectionError, ConnectionClosedError, ConnectTimeoutError, ReadTimeoutError),
    ):
        return True
    if isinstance(exc, ClientError):
        code = _error_code(exc)
        return code in {"403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch",
                       "ExpiredToken", "InvalidToken", "NoSuchBucket"} or code.startswith("5")
    return False


def _head_object(s3: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        response = s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    return dict(response) if isinstance(response, dict) else {}


def _expected_metadata(
    *,
    md5: str,
    page_number: int,
    role: str,
    variant: str,
) -> dict[str, str]:
    return {
        "source-md5": md5,
        "recipe-version": PREVIEW_RECIPE_VERSION,
        "page-number": str(int(page_number)),
        "role": role,
        "variant": variant,
    }


def _matching_remote(
    s3: Any,
    *,
    bucket: str,
    key: str,
    metadata: dict[str, str],
) -> dict[str, Any] | None:
    head = _head_object(s3, bucket, key)
    if head is None:
        return None
    remote_metadata = head.get("Metadata")
    if not isinstance(remote_metadata, dict):
        return None
    normalized = {str(key).lower(): str(value) for key, value in remote_metadata.items()}
    if any(normalized.get(key) != value for key, value in metadata.items()):
        return None
    if int(head.get("ContentLength") or 0) <= 0:
        return None
    for required_integer in ("width", "height", "quality"):
        try:
            if int(normalized.get(required_integer) or 0) <= 0:
                return None
        except ValueError:
            return None
    return head


def _select_detected_pages(
    pdf_path: Path,
    *,
    page_detector: Any,
    boundary: Callable[[], None],
) -> tuple[int, list[PreviewPage], dict[int, PageAssessment]]:
    assessments: dict[int, PageAssessment] = {}
    with fitz.open(pdf_path) as document:
        page_count = int(document.page_count)

        def is_useful(page_number: int) -> bool:
            assessment = assessments.get(page_number)
            if assessment is None:
                boundary()
                image = _render_page_image(document, page_number)
                assessment = page_detector.assess(image, page_number=page_number)
                assessments[page_number] = assessment
            return assessment.useful

        selected = select_informative_preview_pages(
            page_count,
            is_useful=is_useful,
        )
    return page_count, selected, assessments


def process_book(
    md5: str,
    *,
    source_location: tuple[str, str],
    object_prefix: str,
    settings: PreviewGenerationSettings,
    source_s3: Any,
    target_s3: Any,
    page_detector: Any,
    boundary: Callable[[], None],
    log: Callable[[str], None],
) -> BookPreviewResult:
    """Render one verified source; return explicit pages only after all uploads verify."""
    boundary()
    pdf_path, downloaded = ensure_cached_pdf(
        md5, cache_dir=settings.cache_dir,
        source_bucket=source_location[0], source_key=source_location[1],
        s3=source_s3, cache_max_bytes=settings.cache_max_bytes,
    )
    boundary()
    page_count, selected_pages, assessments = _select_detected_pages(
        pdf_path, page_detector=page_detector, boundary=boundary,
    )
    for page_number, assessment in assessments.items():
        log(
            f"library previews: classify md5={md5} page={page_number} "
            f"useful={str(assessment.useful).lower()} "
            f"classes={list(assessment.detected_classes)} "
            f"seconds={assessment.inference_seconds:.3f}"
        )
    log(f"library previews: process md5={md5} pages={page_count} expected_previews={len(selected_pages)}")
    uploaded = reused = 0
    pages = []
    book_workspace = settings.workspace / md5
    for page in selected_pages:
        rendered = None
        keys = {}
        for variant in ("small", "large"):
            boundary()
            key = object_prefix + preview_object_key(md5, page.object_alias, variant)
            keys[f"{variant}_key"] = key
            metadata = _expected_metadata(
                md5=md5, page_number=page.page_number, role=page.role, variant=variant,
            )
            head = _matching_remote(target_s3, bucket=settings.target_bucket, key=key, metadata=metadata)
            if head is not None:
                reused += 1
                log(f"library previews: reuse md5={md5} role={page.role} variant={variant} key={key}")
                continue
            if rendered is None:
                rendered = render_page_variants(
                    pdf_path, page_number=page.page_number,
                    object_alias=page.object_alias, output_dir=book_workspace,
                )
            output = rendered[variant]
            boundary()
            target_s3.upload_file(
                str(output.path), settings.target_bucket, key,
                ExtraArgs={
                    "ContentType": "image/webp",
                    "CacheControl": config_text("previews", "cache_control"),
                    "Metadata": {**metadata, "width": str(output.width),
                                 "height": str(output.height), "quality": str(output.quality)},
                },
                Config=sequential_transfer_config(),
            )
            head = _matching_remote(target_s3, bucket=settings.target_bucket, key=key, metadata=metadata)
            if head is None:
                raise RuntimeError(f"S3 verification failed after upload: {key}")
            uploaded += 1
            log(f"library previews: uploaded md5={md5} role={page.role} page={page.page_number} "
                f"variant={variant} key={key} bytes={head.get('ContentLength') or 0}")
        pages.append({"role": page.role, "page_number": page.page_number, **keys})
    boundary()
    return BookPreviewResult(
        md5=md5, source_page_count=page_count, pages=tuple(pages),
        uploaded_objects=uploaded, reused_objects=reused, downloaded_source=downloaded,
        inspected_pages=len(assessments), rejected_pages=sum(not item.useful for item in assessments.values()),
        selected_pages=len(selected_pages), inference_seconds=sum(item.inference_seconds for item in assessments.values()),
    )


__all__ = [
    "RenderedVariant",
    "BookPreviewResult",
    "PreviewGenerationSettings",
    "ensure_cached_pdf",
    "process_book",
    "render_page_variants",
]
