"""Run resumable rich-content extraction for non-PDF documents."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import signal
import sys
from typing import Any, Callable, Mapping
import zipfile


def _bootstrap_repo_root() -> None:
    root = Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_bootstrap_repo_root()

import requests  # noqa: E402
from boto3 import Session  # noqa: E402
from botocore.config import Config  # noqa: E402

from app.db import Database  # noqa: E402
from app.document_storage import (  # noqa: E402
    DocumentStorageSettings,
    download_cached_primary_document,
    find_valid_cache_file,
    load_document_storage_settings,
    normalized_extension,
    object_url,
)
from app.modules.library.non_pdf_extraction import (  # noqa: E402
    EXTRACTOR_VERSION,
    PreparedExtraction,
    UnsupportedDocumentFormat,
    prepare_extraction,
    render_markdown,
    require_converter_binaries,
    validate_rendered_markdown,
)
from app.modules.library.non_pdf_repository import (  # noqa: E402
    MAX_AUTOMATIC_ATTEMPTS,
    NonPdfExtractionRepository,
)
from app.run_artifact_channel import emit_run_artifact  # noqa: E402
from app.runtime_config import load_runtime_config  # noqa: E402
from app.settings import load_settings  # noqa: E402


TASK_ID = "library.extract_non_pdf"
PANEL_ID = "library"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract rich non-PDF content")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--per-mime-limit", type=int, default=None)
    parser.add_argument(
        "--retry-known-failures",
        action="store_true",
        help="Explicitly retry deferred and exhausted failures",
    )
    return parser.parse_args()


_DEFERRED_FAILURE_MARKERS = (
    "Extracted document contains only images; OCR required",
    "Rendered Markdown validation failed:",
    "LibreOffice produced 0 DOCX files",
    "couldn't unpack docx container:",
)


def _failure_status(exc: Exception) -> str:
    message = str(exc)
    if any(marker in message for marker in _DEFERRED_FAILURE_MARKERS):
        return "deferred"
    return "failed"


def _run_id() -> int:
    raw = str(os.environ.get("MANZARA_TASK_RUN_ID") or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        raise RuntimeError("MANZARA_TASK_RUN_ID is required")
    return int(raw)


def _s3_client(storage: DocumentStorageSettings) -> Any:
    return Session().client(
        "s3",
        aws_access_key_id=storage.primary.access_key_id,
        aws_secret_access_key=storage.primary.secret_access_key,
        endpoint_url=storage.primary.endpoint_url,
        region_name=storage.primary.region_name,
        config=Config(
            signature_version="s3v4",
            connect_timeout=10,
            read_timeout=120,
            retries={"mode": "standard", "total_max_attempts": 3},
            s3={"addressing_style": "path"},
        ),
    )


def _matching_object(
    s3: Any,
    *,
    bucket: str,
    key: str,
    source_md5: str,
    extractor_version: str,
) -> dict[str, Any] | None:
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001
        response = getattr(exc, "response", {})
        code = str(response.get("Error", {}).get("Code") or "") if isinstance(response, dict) else ""
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    metadata = head.get("Metadata") if isinstance(head.get("Metadata"), dict) else {}
    if (
        str(metadata.get("source-md5") or "").lower() != source_md5.lower()
        or str(metadata.get("extractor-version") or "") != extractor_version
        or int(head.get("ContentLength") or 0) <= 0
    ):
        return None
    return head


def _public_object_available(url: str) -> bool:
    try:
        response = requests.head(url, allow_redirects=True, timeout=(10, 30))
        return response.status_code == 200
    except requests.RequestException:
        return False


def _write_content_archive(md5: str, markdown: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    info = zipfile.ZipInfo(f"{md5}.md", date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    with zipfile.ZipFile(destination, "w", compresslevel=9) as archive:
        archive.writestr(info, markdown.encode("utf-8"))
    return destination


def _progress(current: int, total: int, counters: Mapping[str, int]) -> dict[str, Any]:
    return {
        "current": int(current),
        "total": int(total),
        "percent": 100 if total == 0 else round(current / total * 100, 2),
        **{key: int(value) for key, value in counters.items()},
    }


def _mime_key(value: str) -> str:
    return str(value or "").strip().lower() or "unknown"


def _publish_progress(
    db: Database, run_id: int, current: int, total: int, counters: Mapping[str, int]
) -> None:
    payload = _progress(current, total, counters)
    db.update_run_progress(run_id, payload)
    db.insert_event(
        "task.progress",
        task_id=TASK_ID,
        run_id=run_id,
        panel_id=PANEL_ID,
        payload={"status": "running", "progress": payload},
    )


def _upload_assets(
    prepared: PreparedExtraction,
    *,
    md5: str,
    s3: Any,
    storage: DocumentStorageSettings,
) -> tuple[dict[str, str], int, int]:
    urls: dict[str, str] = {}
    uploaded = reused = 0
    for asset in prepared.assets:
        key = f"{md5}/{asset.ordinal}{asset.path.suffix.lower()}"
        head = _matching_object(
            s3,
            bucket=storage.content_images_bucket,
            key=key,
            source_md5=md5,
            extractor_version=EXTRACTOR_VERSION,
        )
        if head is None:
            s3.upload_file(
                str(asset.path),
                storage.content_images_bucket,
                key,
                ExtraArgs={
                    "ContentType": _image_content_type(asset.path.suffix),
                    # Stable short keys are replaced when the extractor changes,
                    # so they must not carry an immutable browser cache policy.
                    "CacheControl": "public, max-age=3600",
                    "Metadata": {
                        "source-md5": md5,
                        "extractor-version": EXTRACTOR_VERSION,
                        "asset-ordinal": str(asset.ordinal),
                    },
                },
            )
            head = _matching_object(
                s3,
                bucket=storage.content_images_bucket,
                key=key,
                source_md5=md5,
                extractor_version=EXTRACTOR_VERSION,
            )
            if head is None:
                raise RuntimeError(f"Embedded image verification failed: {key}")
            uploaded += 1
        else:
            reused += 1
        url = object_url(storage.primary.endpoint_url, storage.content_images_bucket, key)
        if not _public_object_available(url):
            raise RuntimeError(f"Embedded image is not publicly readable: {key}")
        urls[asset.source_ref] = url
    return urls, uploaded, reused


def _expected_asset_urls(
    prepared: PreparedExtraction,
    *,
    md5: str,
    storage: DocumentStorageSettings,
) -> dict[str, str]:
    return {
        asset.source_ref: object_url(
            storage.primary.endpoint_url,
            storage.content_images_bucket,
            f"{md5}/{asset.ordinal}{asset.path.suffix.lower()}",
        )
        for asset in prepared.assets
    }


def _delete_stale_assets(
    s3: Any,
    *,
    bucket: str,
    md5: str,
    expected_keys: set[str],
) -> int:
    prefix = f"{md5}/"
    existing: list[str] = []
    continuation: str | None = None
    while True:
        request: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if continuation:
            request["ContinuationToken"] = continuation
        response = s3.list_objects_v2(**request)
        existing.extend(
            str(item.get("Key") or "")
            for item in response.get("Contents", [])
            if str(item.get("Key") or "")
        )
        if not response.get("IsTruncated"):
            break
        continuation = str(response.get("NextContinuationToken") or "")
        if not continuation:
            raise RuntimeError(f"Missing continuation token for image prefix {prefix}")
    stale = sorted(set(existing) - set(expected_keys))
    for key in stale:
        s3.delete_object(Bucket=bucket, Key=key)
    return len(stale)


def _image_content_type(suffix: str) -> str:
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }.get(str(suffix).lower(), "application/octet-stream")


def run_extraction(
    *,
    repository: NonPdfExtractionRepository,
    db: Database,
    s3: Any,
    storage: DocumentStorageSettings,
    workspace: Path,
    run_id: int,
    should_stop: Callable[[], bool],
    limit: int | None = None,
    per_mime_limit: int | None = None,
    retry_known_failures: bool = False,
) -> dict[str, Any]:
    candidates = repository.list_candidates(
        extractor_version=EXTRACTOR_VERSION,
        limit=limit,
        per_mime_limit=per_mime_limit,
        retry_known_failures=retry_known_failures,
    )
    total = len(candidates)
    counters: Counter[str] = Counter(
        ready=0, failed=0, deferred=0, unsupported=0, downloaded_sources=0,
        reused_sources=0, uploaded_images=0, reused_images=0,
        uploaded_archives=0, reused_archives=0, checkpoint_raced=0,
        deleted_stale_images=0,
    )
    formats: Counter[str] = Counter()
    mime_outcomes: defaultdict[str, Counter[str]] = defaultdict(Counter)
    _publish_progress(db, run_id, 0, total, counters)
    print(
        f"non-pdf extraction: start run_id={run_id} version={EXTRACTOR_VERSION} "
        f"candidates={total} content_bucket={storage.content_bucket} "
        f"images_bucket={storage.content_images_bucket} "
        f"per_mime_limit={per_mime_limit} "
        f"retry_known_failures={retry_known_failures}",
        flush=True,
    )
    processed = 0
    for candidate in candidates:
        if should_stop():
            break
        repository.start_attempt(
            candidate.md5, extractor_version=EXTRACTOR_VERSION, run_id=run_id
        )
        doc_workspace = workspace / candidate.md5
        doc_workspace.mkdir(parents=True, exist_ok=True)
        detected: str | None = None
        try:
            extension = normalized_extension(candidate.source_path, candidate.mime_type)
            cached_before = find_valid_cache_file(
                storage.cache_path, candidate.md5
            ) is not None
            source = download_cached_primary_document(
                settings=storage,
                s3=s3,
                document_url=candidate.document_url,
                expected_md5=candidate.md5,
                expected_size=candidate.primary_storage_size,
                extension=extension,
            )
            counters["reused_sources" if cached_before else "downloaded_sources"] += 1
            prepared = prepare_extraction(
                source,
                workspace=doc_workspace,
                mime_type=candidate.mime_type,
                source_path=candidate.source_path,
            )
            detected = prepared.detected_format
            formats[detected] += 1
            image_urls = _expected_asset_urls(
                prepared, md5=candidate.md5, storage=storage
            )
            markdown = render_markdown(prepared, asset_urls=image_urls)
            validate_rendered_markdown(
                prepared, markdown, asset_urls=image_urls
            )
            uploaded_urls, uploaded_images, reused_images = _upload_assets(
                prepared, md5=candidate.md5, s3=s3, storage=storage
            )
            if uploaded_urls != image_urls:
                raise RuntimeError("Uploaded image URL manifest changed after validation")
            counters["uploaded_images"] += uploaded_images
            counters["reused_images"] += reused_images
            archive_path = _write_content_archive(
                candidate.md5, markdown, doc_workspace / f"{candidate.md5}.zip"
            )
            key = f"{candidate.md5}.zip"
            head = _matching_object(
                s3,
                bucket=storage.content_bucket,
                key=key,
                source_md5=candidate.md5,
                extractor_version=EXTRACTOR_VERSION,
            )
            if head is None:
                s3.upload_file(
                    str(archive_path),
                    storage.content_bucket,
                    key,
                    ExtraArgs={
                        "ContentType": "application/zip",
                        "Metadata": {
                            "source-md5": candidate.md5,
                            "extractor-version": EXTRACTOR_VERSION,
                            "detected-format": detected,
                            "asset-count": str(len(prepared.assets)),
                        },
                    },
                )
                head = _matching_object(
                    s3,
                    bucket=storage.content_bucket,
                    key=key,
                    source_md5=candidate.md5,
                    extractor_version=EXTRACTOR_VERSION,
                )
                if head is None:
                    raise RuntimeError(f"Content archive verification failed: {key}")
                counters["uploaded_archives"] += 1
            else:
                counters["reused_archives"] += 1
            url = object_url(storage.primary.endpoint_url, storage.content_bucket, key)
            if not _public_object_available(url):
                raise RuntimeError(f"Content archive is not publicly readable: {key}")
            if repository.save_success(
                candidate,
                extractor_version=EXTRACTOR_VERSION,
                detected_format=detected,
                run_id=run_id,
                content_url=url,
            ):
                expected_image_keys = {
                    f"{candidate.md5}/{asset.ordinal}{asset.path.suffix.lower()}"
                    for asset in prepared.assets
                }
                counters["deleted_stale_images"] += _delete_stale_assets(
                    s3,
                    bucket=storage.content_images_bucket,
                    md5=candidate.md5,
                    expected_keys=expected_image_keys,
                )
                counters["ready"] += 1
                mime_outcomes[_mime_key(candidate.mime_type)]["ready"] += 1
                print(
                    f"non-pdf extraction: ready md5={candidate.md5} "
                    f"format={detected} images={len(prepared.assets)} url={url}",
                    flush=True,
                )
            else:
                counters["checkpoint_raced"] += 1
                mime_outcomes[_mime_key(candidate.mime_type)]["checkpoint_raced"] += 1
                print(
                    f"non-pdf extraction: checkpoint skipped md5={candidate.md5} "
                    "reason=source or content row changed",
                    flush=True,
                )
        except UnsupportedDocumentFormat as exc:
            detected = exc.detected_format
            formats[detected] += 1
            counters["unsupported"] += 1
            mime_outcomes[_mime_key(candidate.mime_type)]["unsupported"] += 1
            repository.mark_outcome(
                candidate.md5,
                extractor_version=EXTRACTOR_VERSION,
                detected_format=detected,
                status="unsupported",
                run_id=run_id,
                error_text=str(exc),
            )
            print(
                f"non-pdf extraction: unsupported md5={candidate.md5} format={detected}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            status = _failure_status(exc)
            counters[status] += 1
            mime_outcomes[_mime_key(candidate.mime_type)][status] += 1
            repository.mark_outcome(
                candidate.md5,
                extractor_version=EXTRACTOR_VERSION,
                detected_format=detected,
                status=status,
                run_id=run_id,
                error_text=f"{type(exc).__name__}: {exc}",
            )
            print(
                f"non-pdf extraction: {status} md5={candidate.md5} "
                f"format={detected or 'unknown'} error={type(exc).__name__}: {exc}",
                flush=True,
            )
        processed += 1
        _publish_progress(db, run_id, processed, total, counters)
        if should_stop():
            print(
                "non-pdf extraction: graceful stop boundary reached after current document",
                flush=True,
            )
            break
    summary = {
        "kind": "library.non_pdf_extraction_summary",
        "extractor_version": EXTRACTOR_VERSION,
        "per_mime_limit": per_mime_limit,
        "max_automatic_attempts": MAX_AUTOMATIC_ATTEMPTS,
        "retry_known_failures": bool(retry_known_failures),
        "processed": processed,
        "total": total,
        **dict(counters),
        "formats": dict(sorted(formats.items())),
        "mime_outcomes": {
            mime: dict(sorted(outcomes.items()))
            for mime, outcomes in sorted(mime_outcomes.items())
        },
        "stopped": bool(should_stop()),
    }
    print(
        f"non-pdf extraction: final {json.dumps(summary, ensure_ascii=False, sort_keys=True)}",
        flush=True,
    )
    return summary


def main() -> int:
    args = _parse_args()
    run_id = _run_id()
    settings = load_settings()
    storage = load_document_storage_settings(load_runtime_config())
    if not storage.content_bucket or not storage.content_images_bucket:
        raise RuntimeError(
            "documents.primary_storage.bucket.content and content_images are required"
        )
    require_converter_binaries()
    s3 = _s3_client(storage)
    s3.head_bucket(Bucket=storage.content_bucket)
    s3.head_bucket(Bucket=storage.content_images_bucket)
    workspace = Path(
        os.environ.get("MANZARA_ARTIFACTS_ROOT", "~/.manzara")
    ).expanduser() / "library" / "non-pdf-extraction" / f"run-{run_id}"
    workspace.mkdir(parents=True, exist_ok=True)
    repository = NonPdfExtractionRepository(
        settings.database_url, schema=settings.database_schema
    )
    db = Database(settings.database_url, schema=settings.database_schema)
    stop = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop["requested"] = True
        print(
            "non-pdf extraction: graceful stop requested; finishing current document",
            flush=True,
        )

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        summary = run_extraction(
            repository=repository,
            db=db,
            s3=s3,
            storage=storage,
            workspace=workspace,
            run_id=run_id,
            should_stop=lambda: bool(stop["requested"]),
            limit=args.limit,
            per_mime_limit=args.per_mime_limit,
            retry_known_failures=args.retry_known_failures,
        )
        emit_run_artifact(summary)
        return 0
    finally:
        repository.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
