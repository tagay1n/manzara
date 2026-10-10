"""Run resumable rich-content extraction for non-PDF documents."""

from __future__ import annotations

import json
import zipfile
from collections import Counter, defaultdict
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

import requests

from app.artifacts import workspace_dir
from app.catalog.contracts import CatalogConflict
from app.db import Database
from app.document_storage import (
    DocumentStorageSettings,
    download_cached_primary_document,
    find_valid_cache_file,
    load_document_storage_settings,
    normalized_extension,
    object_url,
    prune_document_cache,
)
from app.modules.library.corrupt_document import (
    CorruptDocumentError,
    build_corrupt_cleanup_plan,
)
from app.modules.library.google_doc_conversion import (
    GoogleDriveDocxConverter,
)
from app.modules.library.google_presentation_conversion import (
    GoogleDrivePptxConverter,
)
from app.modules.library.non_pdf_extraction import (
    EXTRACTOR_VERSION,
    PreparedExtraction,
    UnsupportedDocumentFormat,
    prepare_extraction,
    render_markdown,
    require_converter_binaries,
    validate_rendered_markdown,
)
from app.modules.library.non_pdf_repository import (
    NonPdfCandidate,
    NonPdfExtractionRepository,
)
from app.modules.library.non_pdf_types import (
    HTML_EXTRACTOR_VERSION,
    MOBI_EXTRACTOR_VERSION,
    ODT_EXTRACTOR_VERSION,
    POWERPOINT_EXTRACTOR_VERSION,
    SPREADSHEET_EXTRACTOR_VERSION,
    DeferredDocumentExtraction,
    extractor_version_for_format,
)
from app.operational_state import OperationalStateStore
from app.repositories.document_cleanup import (
    DocumentCleanupRepository,
)
from app.runtime_config import config_integer, config_text, load_runtime_config
from app.s3_transfer import create_s3_client, sequential_transfer_config
from app.task_runtime.contracts import RunContext

TASK_ID = "library.extract_non_pdf"


_DEFERRED_FAILURE_MARKERS = (
    "Extracted document contains only images; OCR required",
    "Rendered Markdown validation failed:",
    "LibreOffice produced 0 DOCX files",
    "couldn't unpack docx container:",
)


def _failure_status(exc: Exception) -> str:
    if isinstance(exc, DeferredDocumentExtraction):
        return "deferred"
    message = str(exc)
    if any(marker in message for marker in _DEFERRED_FAILURE_MARKERS):
        return "deferred"
    return "failed"


def _record_pptx_inspection(
    artifact: Callable[[dict[str, Any]], None],
    *,
    workspace: Path,
    md5: str,
    counters: Counter[str],
) -> None:
    """Persist compact findings and link the retained, detailed inspection report."""
    path = workspace / "pptx-inspection.json"
    if not path.exists():
        return
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("source_format") == "powerpoint":
        counters["powerpoint_inspected"] += int(report["inspection_complete"])
        counters["powerpoint_visual_decks"] += int(
            bool(report["image_slide_count"] or report["unsupported_visual_slide_count"])
        )
        counters["powerpoint_ambiguous_decks"] += int(
            bool(report.get("ambiguous_layout_slide_count"))
        )
    else:
        counters["pptx_inspected"] += int(report["inspection_complete"])
        counters["pptx_image_decks"] += int(bool(report["image_slide_count"]))
        counters["pptx_unsupported_visual_decks"] += int(
            bool(report["unsupported_visual_slide_count"])
        )
        counters["pptx_empty_decks"] += int("pptx_no_text" in report["reasons"])
    artifact({
        **{key: value for key, value in report.items() if key != "slides"},
        "kind": "library.non_pdf_inspection",
        "md5": md5,
        "report_path": str(path),
    })


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
        code = (
            str(response.get("Error", {}).get("Code") or "")
            if isinstance(response, dict)
            else ""
        )
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
        response = requests.head(url, allow_redirects=True, timeout=(config_integer("non_pdf", "link_connect_timeout_seconds"), config_integer("non_pdf", "link_read_timeout_seconds")))
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
    db: Database, run_id: int, current: int, total: int, counters: Mapping[str, int], *, force: bool = False,
) -> None:
    payload = _progress(current, total, counters)
    db.publish_run_progress(
        run_id=run_id,
        progress=payload,
        force=force,
    )


def _upload_assets(
    prepared: PreparedExtraction,
    *,
    md5: str,
    s3: Any,
    storage: DocumentStorageSettings,
    extractor_version: str = EXTRACTOR_VERSION,
    generation_id: str,
) -> tuple[dict[str, str], int, int]:
    urls: dict[str, str] = {}
    uploaded = reused = 0
    for asset in prepared.assets:
        key = f"{md5}/{generation_id}/{asset.ordinal}{asset.path.suffix.lower()}"
        head = _matching_object(
            s3,
            bucket=storage.content_images_bucket,
            key=key,
            source_md5=md5,
            extractor_version=extractor_version,
        )
        if head is None:
            s3.upload_file(
                str(asset.path),
                storage.content_images_bucket,
                key,
                ExtraArgs={
                    "ContentType": _image_content_type(asset.path.suffix),
                    "CacheControl": config_text("non_pdf", "cache_control"),
                    "Metadata": {
                        "source-md5": md5,
                        "extractor-version": extractor_version,
                        "asset-ordinal": str(asset.ordinal),
                    },
                },
                Config=sequential_transfer_config(),
            )
            head = _matching_object(
                s3,
                bucket=storage.content_images_bucket,
                key=key,
                source_md5=md5,
                extractor_version=extractor_version,
            )
            if head is None:
                raise RuntimeError(f"Embedded image verification failed: {key}")
            uploaded += 1
        else:
            reused += 1
        url = object_url(
            storage.primary.endpoint_url, storage.content_images_bucket, key
        )
        if not _public_object_available(url):
            raise RuntimeError(f"Embedded image is not publicly readable: {key}")
        urls[asset.source_ref] = url
    return urls, uploaded, reused


def _expected_asset_urls(
    prepared: PreparedExtraction,
    *,
    md5: str,
    storage: DocumentStorageSettings,
    generation_id: str,
) -> dict[str, str]:
    return {
        asset.source_ref: object_url(
            storage.primary.endpoint_url,
            storage.content_images_bucket,
            f"{md5}/{generation_id}/{asset.ordinal}{asset.path.suffix.lower()}",
        )
        for asset in prepared.assets
    }


def _delete_stale_assets(
    s3: Any,
    *,
    bucket: str,
    md5: str,
    expected_keys: set[str],
    generation_id: str,
) -> int:
    prefix = f"{md5}/{generation_id}/"
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
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(str(suffix).lower(), "application/octet-stream")


@dataclass
class _ItemState:
    candidate: NonPdfCandidate
    workspace: Path
    extractor_version: str
    detected_format: str | None = None
    committed: bool = False


@dataclass(frozen=True)
class _LocalContent:
    prepared: PreparedExtraction
    asset_urls: dict[str, str]
    archive_path: Path


class _ExtractionProcessor:
    """Own preparation and publication while the caller owns item boundaries."""

    def __init__(
        self, *, repository: NonPdfExtractionRepository,
        cleanup_repository: Any | None, s3: Any, storage: DocumentStorageSettings,
        workspace: Path, run_id: int, generation_id: str,
        log: Callable[[str], None], artifact: Callable[[dict[str, Any]], None],
        counters: Counter[str], formats: Counter[str],
        mime_outcomes: defaultdict[str, Counter[str]],
    ) -> None:
        self.repository = repository
        self.cleanup_repository = cleanup_repository
        self.s3 = s3
        self.storage = storage
        self.workspace = workspace
        self.run_id = run_id
        self.generation_id = generation_id
        self.log = log
        self.artifact = artifact
        self.counters = counters
        self.formats = formats
        self.mime_outcomes = mime_outcomes
        self.google_doc_converter = GoogleDriveDocxConverter()
        self.google_presentation_converter = GoogleDrivePptxConverter()

    def _prepare_local_content(self, state: _ItemState) -> _LocalContent:
        """Validate and retain local content before acquiring publication locks."""

        def record_detection(inspection) -> None:
            detected_format = inspection.format
            state.detected_format = detected_format
            state.extractor_version = extractor_version_for_format(detected_format)
            state.candidate = self.repository.record_detected_source(
                state.candidate,
                detected_format=detected_format,
                extractor_version=state.extractor_version,
                verified_mime_type=inspection.verified_mime_type,
            )

        extension = normalized_extension(state.candidate.source_path, state.candidate.mime_type)
        cached_before = (
            find_valid_cache_file(self.storage.cache_path, state.candidate.md5) is not None
        )
        source = download_cached_primary_document(
            settings=self.storage,
            s3=self.s3,
            document_url=state.candidate.document_url,
            expected_md5=state.candidate.md5,
            expected_size=state.candidate.primary_storage_size,
            extension=extension,
        )
        self.counters["reused_sources" if cached_before else "downloaded_sources"] += 1
        prepared = prepare_extraction(
            source,
            workspace=state.workspace,
            mime_type=state.candidate.mime_type,
            source_path=state.candidate.source_path,
            legacy_doc_converter=self.google_doc_converter,
            legacy_presentation_converter=self.google_presentation_converter,
            on_detected=record_detection,
        )
        if prepared.legacy_conversion:
            self.counters[f"{prepared.legacy_conversion}_converted"] += 1
        state.detected_format = prepared.detected_format
        if state.detected_format == "powerpoint" and prepared.legacy_conversion:
            self.counters[f"powerpoint_{prepared.legacy_conversion}_converted"] += 1
        if state.detected_format in {"pptx", "powerpoint"}:
            _record_pptx_inspection(
                self.artifact,
                workspace=state.workspace,
                md5=state.candidate.md5,
                counters=self.counters,
            )
        self.formats[state.detected_format] += 1
        image_urls = _expected_asset_urls(
            prepared, md5=state.candidate.md5, storage=self.storage, generation_id=self.generation_id
        )
        markdown = render_markdown(prepared, asset_urls=image_urls)
        validate_rendered_markdown(prepared, markdown, asset_urls=image_urls)
        archive_path = _write_content_archive(
            state.candidate.md5, markdown, state.workspace / f"{state.candidate.md5}.zip"
        )
        self.artifact({
            "kind": "library.non_pdf_local_content",
            "md5": state.candidate.md5,
            "detected_format": state.detected_format,
            "extractor_version": state.extractor_version,
            "legacy_conversion": prepared.legacy_conversion,
            "markdown_path": str(state.workspace / "final.md"),
            "unformatted_path": str(state.workspace / "unformatted.md"),
            "archive_path": str(archive_path),
            "generation_id": self.generation_id,
            "validation_path": str(state.workspace / "validation.json"),
        })
        return _LocalContent(prepared, image_urls, archive_path)

    def _publish_content(self, state: _ItemState, local: _LocalContent) -> str:
        """Hold publication locks through remote verification and catalog commit."""
        prepared, image_urls, archive_path = local.prepared, local.asset_urls, local.archive_path
        with self.repository.publication(state.candidate) as conn:
            uploaded_urls, uploaded_images, reused_images = _upload_assets(
                prepared,
                md5=state.candidate.md5,
                s3=self.s3,
                storage=self.storage,
                extractor_version=state.extractor_version,
                generation_id=self.generation_id,
            )
            if uploaded_urls != image_urls:
                raise RuntimeError(
                    "Uploaded image URL manifest changed after validation"
                )
            self.counters["uploaded_images"] += uploaded_images
            self.counters["reused_images"] += reused_images
            key = f"{state.candidate.md5}/{self.generation_id}.zip"
            head = _matching_object(
                self.s3,
                bucket=self.storage.content_bucket,
                key=key,
                source_md5=state.candidate.md5,
                extractor_version=state.extractor_version,
            )
            if head is None:
                self.s3.upload_file(
                    str(archive_path),
                    self.storage.content_bucket,
                    key,
                    ExtraArgs={
                        "ContentType": "application/zip",
                        "Metadata": {
                            "source-md5": state.candidate.md5,
                            "extractor-version": state.extractor_version,
                            "detected-format": state.detected_format,
                            "asset-count": str(len(prepared.assets)),
                        },
                    },
                    Config=sequential_transfer_config(),
                )
                head = _matching_object(
                    self.s3,
                    bucket=self.storage.content_bucket,
                    key=key,
                    source_md5=state.candidate.md5,
                    extractor_version=state.extractor_version,
                )
                if head is None:
                    raise RuntimeError(f"Content archive verification failed: {key}")
                self.counters["uploaded_archives"] += 1
            else:
                self.counters["reused_archives"] += 1
            url = object_url(self.storage.primary.endpoint_url, self.storage.content_bucket, key)
            if not _public_object_available(url):
                raise RuntimeError(f"Content archive is not publicly readable: {key}")
            self.repository.save_success(
                state.candidate, conn=conn, extractor_version=state.extractor_version,
                detected_format=state.detected_format, content_url=url,
                size=int(head["ContentLength"]), etag=head.get("ETag"),
            )
            expected_image_keys = {
                f"{state.candidate.md5}/{self.generation_id}/{asset.ordinal}{asset.path.suffix.lower()}"
                for asset in prepared.assets
            }
            self.counters["deleted_stale_images"] += _delete_stale_assets(
                self.s3, bucket=self.storage.content_images_bucket, md5=state.candidate.md5,
                expected_keys=expected_image_keys, generation_id=self.generation_id,
            )
        return url

    def _mark_outcome(self, state: _ItemState, status: str, *, error_text: str | None = None) -> None:
        self.repository.mark_outcome(
            state.candidate, extractor_version=state.extractor_version,
            detected_format=state.detected_format, status=status,
            run_id=self.run_id, error_text=error_text,
        )

    def process(self, candidate: NonPdfCandidate) -> None:
        state = _ItemState(
            candidate, self.workspace / candidate.md5,
            extractor_version_for_format(candidate.prior_detected_format),
        )
        self.repository.start_attempt(
            candidate.md5, extractor_version=state.extractor_version, run_id=self.run_id,
        )
        self.log(f"non-pdf extraction: item start md5={candidate.md5} source={candidate.source_path}")
        state.workspace.mkdir(parents=True, exist_ok=True)
        (state.workspace / "pptx-inspection.json").unlink(missing_ok=True)
        try:
            local = self._prepare_local_content(state)
            url = self._publish_content(state, local)
            state.committed = True
            self._mark_outcome(state, "ready")
            self.counters["ready"] += 1
            self.counters["pptx_extracted"] += int(state.detected_format == "pptx")
            self.counters["powerpoint_extracted"] += int(state.detected_format == "powerpoint")
            self.mime_outcomes[_mime_key(state.candidate.mime_type)]["ready"] += 1
            self.log(
                f"non-pdf extraction: ready md5={state.candidate.md5} format={state.detected_format} "
                f"conversion={local.prepared.legacy_conversion or 'native'} images={len(local.prepared.assets)} url={url}"
            )
        except CatalogConflict as exc:
            self.counters["checkpoint_raced"] += 1
            self.mime_outcomes[_mime_key(state.candidate.mime_type)]["checkpoint_raced"] += 1
            self._mark_outcome(state, "failed", error_text=str(exc))
            self.log(f"non-pdf extraction: checkpoint conflict md5={state.candidate.md5} reason={exc}")
        except CorruptDocumentError as exc:
            if self.cleanup_repository is None:
                raise RuntimeError(
                    "Corrupt document planning requires a cleanup repository"
                ) from exc
            with self.repository.publication(state.candidate) as conn:
                cleanup_id, created = self.cleanup_repository.enqueue_cleanup(
                    build_corrupt_cleanup_plan(
                        storage=self.storage,
                        md5=state.candidate.md5,
                        source_path=state.candidate.source_path,
                        mime_type=state.candidate.mime_type,
                        source_size=state.candidate.primary_storage_size,
                        task_id=TASK_ID,
                        run_id=self.run_id,
                        error=exc,
                    ), conn=conn,
                )
            self.counters["corrupted" if created else "corrupted_plan_reused"] += 1
            self.mime_outcomes[_mime_key(state.candidate.mime_type)]["corrupted"] += 1
            self._mark_outcome(state, "failed", error_text=f"{type(exc).__name__}: {exc}")
            self.log(
                f"non-pdf extraction: corrupted plan md5={state.candidate.md5} "
                f"cleanup_id={cleanup_id} detector={exc.detector} created={created}"
            )
        except DeferredDocumentExtraction as exc:
            state.detected_format = exc.detected_format
            self.formats[state.detected_format] += 1
            self.counters["deferred"] += 1
            self.mime_outcomes[_mime_key(state.candidate.mime_type)]["deferred"] += 1
            self._mark_outcome(state, "deferred", error_text=str(exc))
            _record_pptx_inspection(
                self.artifact,
                workspace=state.workspace,
                md5=state.candidate.md5,
                counters=self.counters,
            )
            self.log(
                f"non-pdf extraction: deferred md5={state.candidate.md5} reason={exc.reason}"
            )
        except UnsupportedDocumentFormat as exc:
            state.detected_format = exc.detected_format
            self.formats[state.detected_format] += 1
            self.counters["unsupported"] += 1
            self.mime_outcomes[_mime_key(state.candidate.mime_type)]["unsupported"] += 1
            self._mark_outcome(state, "unsupported", error_text=str(exc))
            self.log(
                f"non-pdf extraction: unsupported md5={state.candidate.md5} format={state.detected_format}"
            )
        except Exception as exc:  # noqa: BLE001
            if state.committed:
                raise
            status = _failure_status(exc)
            self.counters[status] += 1
            self.mime_outcomes[_mime_key(state.candidate.mime_type)][status] += 1
            self._mark_outcome(state, status, error_text=f"{type(exc).__name__}: {exc}")
            self.log(
                f"non-pdf extraction: {status} md5={state.candidate.md5} "
                f"format={state.detected_format or 'unknown'} error={type(exc).__name__}: {exc}"
            )


def _build_summary(
    *, workspace: Path, generation_id: str, per_mime_limit: int | None,
    retry_known_failures: bool, processed: int, total: int, counters: Counter[str],
    formats: Counter[str], mime_outcomes: defaultdict[str, Counter[str]],
    should_stop: Callable[[], bool],
) -> dict[str, Any]:
    return {
        "kind": "library.non_pdf_extraction_summary",
        "workspace_path": str(workspace),
        "generation_id": generation_id,
        "extractor_version": EXTRACTOR_VERSION,
        "powerpoint_extractor_version": POWERPOINT_EXTRACTOR_VERSION,
        "spreadsheet_extractor_version": SPREADSHEET_EXTRACTOR_VERSION,
        "odt_extractor_version": ODT_EXTRACTOR_VERSION,
        "mobi_extractor_version": MOBI_EXTRACTOR_VERSION,
        "html_extractor_version": HTML_EXTRACTOR_VERSION,
        "per_mime_limit": per_mime_limit,
        "max_automatic_attempts": config_integer("non_pdf", "max_automatic_attempts"),
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
        "outcome": "stopped" if should_stop() else (
            "failed" if counters["failed"] or counters["checkpoint_raced"] else (
                "deferred" if counters["deferred"] else "completed"
            )
        ),
    }


def run_extraction(
    *,
    repository: NonPdfExtractionRepository,
    cleanup_repository: Any | None = None,
    db: Database,
    s3: Any,
    storage: DocumentStorageSettings,
    workspace: Path,
    run_id: int,
    should_stop: Callable[[], bool],
    log: Callable[[str], None],
    artifact: Callable[[dict[str, Any]], None],
    limit: int | None = None,
    per_mime_limit: int | None = None,
    retry_known_failures: bool = False,
    only_md5s: frozenset[str] | None = None,
) -> dict[str, Any]:
    candidates = repository.list_candidates(
        extractor_version=EXTRACTOR_VERSION,
        powerpoint_version=POWERPOINT_EXTRACTOR_VERSION,
        spreadsheet_version=SPREADSHEET_EXTRACTOR_VERSION,
        odt_version=ODT_EXTRACTOR_VERSION,
        mobi_version=MOBI_EXTRACTOR_VERSION,
        html_version=HTML_EXTRACTOR_VERSION,
        limit=limit,
        per_mime_limit=per_mime_limit,
        retry_known_failures=retry_known_failures,
        only_md5s=only_md5s,
    )
    total = len(candidates)
    generation_id = f"run-{run_id}-{uuid4().hex}"
    counters: Counter[str] = Counter(
        ready=0,
        failed=0,
        deferred=0,
        unsupported=0,
        corrupted=0,
        corrupted_plan_reused=0,
        downloaded_sources=0,
        reused_sources=0,
        uploaded_images=0,
        reused_images=0,
        uploaded_archives=0,
        reused_archives=0,
        checkpoint_raced=0,
        deleted_stale_images=0,
        google_drive_converted=0,
        libreoffice_converted=0,
        pptx_inspected=0,
        pptx_extracted=0,
        pptx_image_decks=0,
        pptx_unsupported_visual_decks=0,
        pptx_empty_decks=0,
        powerpoint_inspected=0,
        powerpoint_extracted=0,
        powerpoint_visual_decks=0,
        powerpoint_ambiguous_decks=0,
        powerpoint_google_converted=0,
        powerpoint_libreoffice_converted=0,
    )
    formats: Counter[str] = Counter()
    mime_outcomes: defaultdict[str, Counter[str]] = defaultdict(Counter)
    _publish_progress(db, run_id, 0, total, counters)
    log(
        f"non-pdf extraction: start run_id={run_id} version={EXTRACTOR_VERSION} "
        f"candidates={total} content_bucket={storage.content_bucket} "
        f"images_bucket={storage.content_images_bucket} "
        f"per_mime_limit={per_mime_limit} "
        f"retry_known_failures={retry_known_failures}"
    )
    processor = _ExtractionProcessor(
        repository=repository, cleanup_repository=cleanup_repository, s3=s3,
        storage=storage, workspace=workspace, run_id=run_id, generation_id=generation_id,
        log=log, artifact=artifact, counters=counters, formats=formats, mime_outcomes=mime_outcomes,
    )
    processed = 0
    for candidate in candidates:
        if should_stop():
            break
        processor.process(candidate)
        processed += 1
        _publish_progress(db, run_id, processed, total, counters)
        if should_stop():
            log("non-pdf extraction: graceful stop boundary reached after current document")
            break
    _publish_progress(db, run_id, processed, total, counters, force=True)
    summary = _build_summary(
        workspace=workspace, generation_id=generation_id, per_mime_limit=per_mime_limit,
        retry_known_failures=retry_known_failures, processed=processed, total=total,
        counters=counters, formats=formats, mime_outcomes=mime_outcomes, should_stop=should_stop,
    )
    log(f"non-pdf extraction: final {json.dumps(summary, ensure_ascii=False, sort_keys=True)}")
    return summary


def execute(context: RunContext) -> dict[str, Any]:
    """Run in the CLI worker with explicit logging, cancellation and local state."""
    if context.should_stop():
        return {"kind": "library.non_pdf_extraction_summary", "outcome": "stopped"}
    with ExitStack() as resources:
        repository = NonPdfExtractionRepository(
            context.db.database_url, schema=context.db.schema,
            runtime=OperationalStateStore(context.db.local_state_path),
        )
        resources.callback(repository.dispose)
        repository.preflight()
        storage = load_document_storage_settings(load_runtime_config())
        if not storage.content_bucket or not storage.content_images_bucket:
            raise RuntimeError("documents.primary_storage.bucket.content and content_images are required")
        require_converter_binaries()
        if context.should_stop():
            return {"kind": "library.non_pdf_extraction_summary", "outcome": "stopped"}
        prune_document_cache(storage.cache_path, max_bytes=storage.cache_max_bytes)
        s3 = create_s3_client(storage.primary, profile="extraction")
        resources.callback(s3.close)
        s3.head_bucket(Bucket=storage.content_bucket)
        s3.head_bucket(Bucket=storage.content_images_bucket)
        cleanup_repository = DocumentCleanupRepository(context.db.database_url, schema=context.db.schema)
        resources.callback(cleanup_repository.dispose)
        return run_extraction(
            repository=repository, cleanup_repository=cleanup_repository,
            db=context.db, s3=s3, storage=storage,
            workspace=workspace_dir("library", "non-pdf-extraction", run_id=context.run_id),
            run_id=context.run_id, should_stop=context.should_stop, log=context.log, artifact=context.artifact,
            limit=context.options.limit, per_mime_limit=context.options.per_mime_limit,
            retry_known_failures=context.options.retry_known_failures,
            only_md5s=frozenset(context.options.only_md5s) if context.options.only_md5s else None,
        )
