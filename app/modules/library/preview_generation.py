"""Rendering and storage primitives for Library PDF previews."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz
from PIL import Image
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from app.document_storage import (
    DEFAULT_DOCUMENT_CACHE_MAX_BYTES,
    DEFAULT_S3_ENDPOINT,
    find_valid_cache_file,
    materialize_cached_document,
    resolve_document_object_location,
)

from app.modules.library.preview_detection import PageAssessment, PreviewModelError
from app.modules.library.previews import (
    PREVIEW_RECIPE_VERSION,
    PreviewPage,
    derive_preview_status,
    preview_object_key,
    preview_pages_from_row,
    select_informative_preview_pages,
)


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

    source_bucket: str
    target_bucket: str
    cache_dir: Path
    workspace: Path
    model_cache_dir: Path | None = None
    source_endpoint_url: str = DEFAULT_S3_ENDPOINT
    source_region_name: str = "ru-central1"
    encryption_key: str = ""
    cache_max_bytes: int = DEFAULT_DOCUMENT_CACHE_MAX_BYTES


@dataclass(frozen=True)
class BookPreviewResult:
    """Structured outcome for one candidate document."""

    md5: str
    status: str
    uploaded_objects: int = 0
    reused_objects: int = 0
    downloaded_source: bool = False
    inspected_pages: int = 0
    rejected_pages: int = 0
    selected_pages: int = 0
    inference_seconds: float = 0.0
    error: str | None = None


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
        1000,
        1500,
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

    small_size = _target_size(large_image.width, large_image.height, 400, 600)
    small_image = large_image.resize(small_size, Image.Resampling.LANCZOS)
    small_path = output_dir / f"{object_alias}s.webp"
    large_path = output_dir / f"{object_alias}l.webp"
    small_image.save(small_path, format="WEBP", quality=80, method=6)
    large_image.save(large_path, format="WEBP", quality=85, method=6)
    return {
        "small": RenderedVariant(
            path=small_path,
            width=small_image.width,
            height=small_image.height,
            quality=80,
        ),
        "large": RenderedVariant(
            path=large_path,
            width=large_image.width,
            height=large_image.height,
            quality=85,
        ),
    }


def ensure_cached_pdf(
    md5: str,
    *,
    cache_dir: Path,
    source_bucket: str,
    source_key: str | None = None,
    s3: Any,
    cache_max_bytes: int = DEFAULT_DOCUMENT_CACHE_MAX_BYTES,
) -> tuple[Path, bool]:
    """Return a hash-verified cached PDF, atomically downloading when absent."""
    digest = str(md5 or "").strip().lower()
    cached_before = find_valid_cache_file(cache_dir, digest)
    if cached_before is not None:
        return cached_before, False

    def download(temporary: Path) -> None:
        s3.download_file(
            str(source_bucket), str(source_key or f"{digest}.pdf"), str(temporary)
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
        return code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"} or code.startswith("5")
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
) -> tuple[int, list[PreviewPage], dict[int, PageAssessment]]:
    assessments: dict[int, PageAssessment] = {}
    with fitz.open(pdf_path) as document:
        page_count = int(document.page_count)

        def is_useful(page_number: int) -> bool:
            assessment = assessments.get(page_number)
            if assessment is None:
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
    candidate: dict[str, Any],
    *,
    repository: Any,
    settings: PreviewGenerationSettings,
    source_s3: Any,
    target_s3: Any,
    page_detector: Any,
    run_id: int | None,
    log: Any,
) -> BookPreviewResult:
    """Generate and checkpoint all expected variants for one applicable PDF."""
    md5 = str(candidate.get("md5") or "").strip().lower()
    started = repository.start_attempt(
        md5,
        recipe_version=PREVIEW_RECIPE_VERSION,
        run_id=run_id,
    )
    page_count = int(started.get("source_page_count") or 0)
    uploaded = 0
    reused = 0
    downloaded = False
    verified_objects = 0
    selected_pages: list[PreviewPage] = []
    assessments: dict[int, PageAssessment] = {}
    book_workspace = settings.workspace / md5
    book_workspace.mkdir(parents=True, exist_ok=True)
    try:
        source_bucket = settings.source_bucket
        source_key = f"{md5}.pdf"
        location = resolve_document_object_location(
            document_url=str(candidate.get("document_url") or "") or None,
            encryption_key=settings.encryption_key,
            endpoint_url=settings.source_endpoint_url,
        )
        if location:
            source_bucket, source_key = location
        pdf_path, downloaded = ensure_cached_pdf(
            md5,
            cache_dir=settings.cache_dir,
            source_bucket=source_bucket,
            source_key=source_key,
            s3=source_s3,
            cache_max_bytes=settings.cache_max_bytes,
        )
        persisted_pages = preview_pages_from_row(started)
        if persisted_pages:
            with fitz.open(pdf_path) as document:
                page_count = int(document.page_count)
            selected_pages = persisted_pages
            log(
                f"library previews: reuse selection md5={md5} "
                f"pages={[page.page_number for page in selected_pages]}"
            )
        else:
            page_count, selected_pages, assessments = _select_detected_pages(
                pdf_path,
                page_detector=page_detector,
            )
            for page_number, assessment in assessments.items():
                log(
                    f"library previews: classify md5={md5} page={page_number} "
                    f"useful={str(assessment.useful).lower()} "
                    f"classes={list(assessment.detected_classes)} "
                    f"seconds={assessment.inference_seconds:.3f}"
                )
        log(
            f"library previews: process md5={md5} pages={page_count} "
            f"expected_previews={len(selected_pages)}"
        )
        repository.checkpoint(
            md5,
            recipe_version=PREVIEW_RECIPE_VERSION,
            source_page_count=page_count,
            selected_pages=selected_pages,
            status="processing",
            run_id=run_id,
        )

        for page in selected_pages:
            rendered: dict[str, RenderedVariant] | None = None
            for variant in ("small", "large"):
                key = preview_object_key(md5, page.object_alias, variant)
                expected_metadata = _expected_metadata(
                    md5=md5,
                    page_number=page.page_number,
                    role=page.role,
                    variant=variant,
                )
                head = _matching_remote(
                    target_s3,
                    bucket=settings.target_bucket,
                    key=key,
                    metadata=expected_metadata,
                )
                if head is not None:
                    reused += 1
                    log(
                        f"library previews: reuse md5={md5} role={page.role} "
                        f"page={page.page_number} variant={variant} key={key}"
                    )
                else:
                    if rendered is None:
                        rendered = render_page_variants(
                            pdf_path,
                            page_number=page.page_number,
                            object_alias=page.object_alias,
                            output_dir=book_workspace,
                        )
                    output = rendered[variant]
                    upload_metadata = {
                        **expected_metadata,
                        "width": str(output.width),
                        "height": str(output.height),
                        "quality": str(output.quality),
                    }
                    target_s3.upload_file(
                        str(output.path),
                        settings.target_bucket,
                        key,
                        ExtraArgs={
                            "ContentType": "image/webp",
                            "CacheControl": "public, max-age=31536000, immutable",
                            "Metadata": upload_metadata,
                        },
                    )
                    head = _matching_remote(
                        target_s3,
                        bucket=settings.target_bucket,
                        key=key,
                        metadata=expected_metadata,
                    )
                    if head is None:
                        raise RuntimeError(f"S3 verification failed after upload: {key}")
                    uploaded += 1
                    log(
                        f"library previews: uploaded md5={md5} role={page.role} "
                        f"page={page.page_number} variant={variant} key={key} "
                        f"bytes={head.get('ContentLength') or 0}"
                    )

                verified_objects += 1
                current_status = derive_preview_status(len(selected_pages), verified_objects)
                repository.checkpoint(
                    md5,
                    recipe_version=PREVIEW_RECIPE_VERSION,
                    source_page_count=page_count,
                    selected_pages=selected_pages,
                    status=current_status,
                    run_id=run_id,
                )

        status = derive_preview_status(len(selected_pages), verified_objects)
        repository.checkpoint(
            md5,
            recipe_version=PREVIEW_RECIPE_VERSION,
            source_page_count=page_count,
            selected_pages=selected_pages,
            status=status,
            run_id=run_id,
        )
        return BookPreviewResult(
            md5=md5,
            status=status,
            uploaded_objects=uploaded,
            reused_objects=reused,
            downloaded_source=downloaded,
            inspected_pages=len(assessments),
            rejected_pages=sum(not item.useful for item in assessments.values()),
            selected_pages=len(selected_pages),
            inference_seconds=sum(item.inference_seconds for item in assessments.values()),
        )
    except Exception as exc:
        status = (
            derive_preview_status(len(selected_pages), verified_objects)
            if page_count > 0 and selected_pages
            else "failed"
        )
        repository.checkpoint(
            md5,
            recipe_version=PREVIEW_RECIPE_VERSION,
            source_page_count=page_count if page_count > 0 else None,
            selected_pages=selected_pages,
            status=status,
            run_id=run_id,
            error_text=str(exc),
        )
        log(f"library previews: failed md5={md5} status={status} error={exc}")
        if _is_storage_fatal(exc) or isinstance(exc, PreviewModelError):
            raise
        return BookPreviewResult(
            md5=md5,
            status=status,
            uploaded_objects=uploaded,
            reused_objects=reused,
            downloaded_source=downloaded,
            inspected_pages=len(assessments),
            rejected_pages=sum(not item.useful for item in assessments.values()),
            selected_pages=len(selected_pages),
            inference_seconds=sum(item.inference_seconds for item in assessments.values()),
            error=str(exc),
        )
__all__ = [
    "RenderedVariant",
    "BookPreviewResult",
    "PreviewGenerationSettings",
    "ensure_cached_pdf",
    "process_book",
    "render_page_variants",
]
