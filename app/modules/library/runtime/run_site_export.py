"""Export the public static Library through the interactive operations CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from app.modules.library.site_export import (
    EXPORT_FORMAT, EXPORT_VERSION, ExportStopped, ExportStorage,
    build_library_export, write_export_bundle,
)
from app.modules.library.site_export_repository import LibrarySiteExportRepository
from app.task_runtime.contracts import RunContext


def _storage(configuration: Mapping[str, Any]) -> ExportStorage:
    # Export needs public URL configuration, never storage/Yandex credentials.
    documents = configuration.get("documents") or {}
    if not isinstance(documents, Mapping):
        raise ValueError("documents configuration must be a mapping")
    primary = documents.get("primary_storage") or {}
    if not isinstance(primary, Mapping):
        raise ValueError("documents.primary_storage configuration must be a mapping")
    buckets = primary.get("bucket") or {}
    if not isinstance(buckets, Mapping):
        raise ValueError("documents.primary_storage.bucket configuration must be a mapping")
    return ExportStorage(
        endpoint_url=str(primary.get("endpoint_url") or "").strip().rstrip("/"),
        public_document_bucket=str(buckets.get("public") or "").strip(),
        public_preview_bucket=str(buckets.get("book_previews") or "").strip(),
        public_content_bucket=str(buckets.get("content") or "").strip(),
    )


def run_export(
    *, repository: LibrarySiteExportRepository, storage: ExportStorage, destination: Path,
    should_stop: Callable[[], bool] = lambda: False,
    log: Callable[[str], None] = lambda _message: None,
    progress: Callable[..., None] = lambda *_args, **_kwargs: None,
) -> dict[str, Any]:
    """Read, validate, and publish a full snapshot at the final safe boundary."""
    progress({"phase": "discovering", "current": 0}, force=True)
    candidates, entities = repository.load_snapshot(should_stop=should_stop, log=log)
    progress({"phase": "processing", "current": 0, "total": len(candidates)}, force=True)

    def document_progress(values):
        progress({**values, "total": len(candidates)})

    export = build_library_export(
        candidates, entities=entities, storage=storage, should_stop=should_stop,
        log=log, progress=document_progress,
    )
    if should_stop():
        raise ExportStopped("Static Library export stopped before publication")
    log(f"static library export: preparing bundle documents={len(export.documents)} excluded={sum(export.exclusions.values())}")
    progress({"phase": "publishing", "current": len(candidates), "total": len(candidates)}, force=True)
    bundle = write_export_bundle(export, destination=destination, should_stop=should_stop)
    summary = {
        "kind": "library.site_export_summary", "format": EXPORT_FORMAT, "version": EXPORT_VERSION,
        "revision": bundle.manifest["revision"], "bundle_path": str(bundle.path), "bundle_sha256": bundle.sha256,
        "documents_published": len(export.documents), "documents_excluded": sum(export.exclusions.values()),
        "entities": len(export.entities), "collections": len(export.collections), "classifications": len(export.classifications),
        "documents_with_previews": sum("preview" in row for row in export.documents),
        "exclusion_reasons": export.exclusions, "stopped": False, "outcome": "completed",
    }
    log(f"static library export: published revision={summary['revision']} bundle={bundle.path}")
    log(f"static library export: completed documents={summary['documents_published']} excluded={summary['documents_excluded']} entities={summary['entities']}")
    return summary


def execute(context: RunContext) -> dict:
    if context.options.workers != 1:
        raise ValueError("Static Library export is sequential; select one worker")
    if (context.options.limit is not None or context.options.per_mime_limit is not None
            or context.options.only_md5s or context.options.retry_known_failures):
        raise ValueError("Static Library export requires the complete inventory; clear candidate/retry options")
    stopped = {"kind": "library.site_export_summary", "format": EXPORT_FORMAT,
               "version": EXPORT_VERSION, "stopped": True, "outcome": "stopped"}
    if context.should_stop():
        return stopped
    from app.artifacts import durable_path
    from app.runtime_config import load_runtime_config

    storage = _storage(load_runtime_config())
    repository = LibrarySiteExportRepository(context.db.database_url, schema=context.db.schema)
    try:
        return run_export(
            repository=repository, storage=storage, destination=durable_path("library", "site-exports"),
            should_stop=context.should_stop, log=context.log, progress=context.progress,
        )
    except ExportStopped:
        context.log("static library export: stopped; previous bundle retained")
        return stopped
    finally:
        repository.dispose()
