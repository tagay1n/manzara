"""Rendering and storage primitives for Library PDF previews."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import shutil
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

from app.document_storage import DEFAULT_S3_ENDPOINT, resolve_document_object_location

from app.modules.library.previews import (
    PREVIEW_RECIPE_VERSION,
    derive_preview_status,
    preview_object_key,
    select_preview_pages,
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
    source_endpoint_url: str = DEFAULT_S3_ENDPOINT
    source_region_name: str = "ru-central1"
    encryption_key: str = ""


@dataclass(frozen=True)
class BookPreviewResult:
    """Structured outcome for one candidate document."""

    md5: str
    status: str
    uploaded_objects: int = 0
    reused_objects: int = 0
    downloaded_source: bool = False
    error: str | None = None


def _target_size(width: int, height: int, max_width: int, max_height: int) -> tuple[int, int]:
    scale = min(float(max_width) / float(width), float(max_height) / float(height))
    return max(1, round(width * scale)), max(1, round(height * scale))


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
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
        large_image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)

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


def calculate_md5(path: Path) -> str:
    """Calculate the content identity used by monocorpus documents."""
    digest = hashlib.md5()  # noqa: S324 - source document identity is MD5.
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_cached_pdf(
    md5: str,
    *,
    cache_dir: Path,
    source_bucket: str,
    source_key: str | None = None,
    s3: Any,
) -> tuple[Path, bool]:
    """Return a hash-verified cached PDF, atomically downloading when absent."""
    digest = str(md5 or "").strip().lower()
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{digest}.pdf"
    if target.exists() and calculate_md5(target) == digest:
        return target, False
    if target.exists():
        target.unlink()

    temporary = cache_dir / f"{digest}.pdf.part"
    temporary.unlink(missing_ok=True)
    try:
        s3.download_file(
            str(source_bucket), str(source_key or f"{digest}.pdf"), str(temporary)
        )
        actual = calculate_md5(temporary)
        if actual != digest:
            raise ValueError(
                f"Downloaded PDF MD5 mismatch for {digest}: expected={digest} actual={actual}"
            )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
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


def _manifest_variant(key: str, head: dict[str, Any]) -> dict[str, Any]:
    metadata = head.get("Metadata") if isinstance(head.get("Metadata"), dict) else {}
    return {
        "key": key,
        "width": int(metadata.get("width") or 0),
        "height": int(metadata.get("height") or 0),
        "quality": int(metadata.get("quality") or 0),
        "bytes": int(head.get("ContentLength") or 0),
        "etag": str(head.get("ETag") or "").strip('"') or None,
    }


def process_book(
    candidate: dict[str, Any],
    *,
    repository: Any,
    settings: PreviewGenerationSettings,
    s3: Any,
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
    manifest = dict(started.get("manifest") or {})
    page_count = int(started.get("source_page_count") or 0)
    uploaded = 0
    reused = 0
    downloaded = False
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
            s3=s3,
        )
        with fitz.open(pdf_path) as document:
            page_count = int(document.page_count)
        selected_pages = select_preview_pages(page_count)
        log(
            f"library previews: process md5={md5} pages={page_count} "
            f"expected_previews={len(selected_pages)}"
        )

        for page in selected_pages:
            rendered: dict[str, RenderedVariant] | None = None
            page_manifest = manifest.get(page.role)
            if not isinstance(page_manifest, dict):
                page_manifest = {"page_number": page.page_number, "variants": {}}
                manifest[page.role] = page_manifest
            page_manifest["page_number"] = page.page_number
            variants_manifest = page_manifest.get("variants")
            if not isinstance(variants_manifest, dict):
                variants_manifest = {}
                page_manifest["variants"] = variants_manifest

            for variant in ("small", "large"):
                key = preview_object_key(md5, page.object_alias, variant)
                expected_metadata = _expected_metadata(
                    md5=md5,
                    page_number=page.page_number,
                    role=page.role,
                    variant=variant,
                )
                head = _matching_remote(
                    s3,
                    bucket=settings.target_bucket,
                    key=key,
                    metadata=expected_metadata,
                )
                if head is not None:
                    variants_manifest[variant] = _manifest_variant(key, head)
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
                    s3.upload_file(
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
                        s3,
                        bucket=settings.target_bucket,
                        key=key,
                        metadata=expected_metadata,
                    )
                    if head is None:
                        raise RuntimeError(f"S3 verification failed after upload: {key}")
                    variants_manifest[variant] = _manifest_variant(key, head)
                    uploaded += 1
                    log(
                        f"library previews: uploaded md5={md5} role={page.role} "
                        f"page={page.page_number} variant={variant} key={key} "
                        f"bytes={head.get('ContentLength') or 0}"
                    )

                current_status = derive_preview_status(page_count, manifest)
                repository.checkpoint(
                    md5,
                    recipe_version=PREVIEW_RECIPE_VERSION,
                    source_page_count=page_count,
                    status=current_status,
                    manifest=manifest,
                    run_id=run_id,
                )

        status = derive_preview_status(page_count, manifest)
        return BookPreviewResult(
            md5=md5,
            status=status,
            uploaded_objects=uploaded,
            reused_objects=reused,
            downloaded_source=downloaded,
        )
    except Exception as exc:
        status = derive_preview_status(page_count, manifest) if page_count > 0 else "failed"
        repository.checkpoint(
            md5,
            recipe_version=PREVIEW_RECIPE_VERSION,
            source_page_count=page_count if page_count > 0 else None,
            status=status,
            manifest=manifest,
            run_id=run_id,
            error_text=str(exc),
        )
        log(f"library previews: failed md5={md5} status={status} error={exc}")
        if _is_storage_fatal(exc):
            raise
        return BookPreviewResult(
            md5=md5,
            status=status,
            uploaded_objects=uploaded,
            reused_objects=reused,
            downloaded_source=downloaded,
            error=str(exc),
        )
    finally:
        shutil.rmtree(book_workspace, ignore_errors=True)


__all__ = [
    "RenderedVariant",
    "BookPreviewResult",
    "PreviewGenerationSettings",
    "calculate_md5",
    "ensure_cached_pdf",
    "process_book",
    "render_page_variants",
]
