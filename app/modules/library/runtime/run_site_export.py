"""Build the versioned public export consumed by the static Library site."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import tarfile
from pathlib import Path
from typing import Any

from app.artifacts import flow_artifacts_dir
from app.document_storage import load_document_storage_settings
from app.modules.library.site_export import (
    EXPORT_FORMAT,
    EXPORT_VERSION,
    ExportStopped,
    ExportStorage,
    build_library_export,
    write_export_bundle,
)
from app.modules.library.site_export_repository import LibrarySiteExportRepository
from app.run_artifact_channel import emit_run_artifact
from app.runtime_config import load_runtime_config
from app.settings import load_settings


TASK_ID = "library.site_export"


def _run_id() -> int:
    raw = str(os.environ.get("MANZARA_TASK_RUN_ID") or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        raise RuntimeError("MANZARA_TASK_RUN_ID is required")
    return int(raw)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(path: Path) -> dict[str, Any]:
    with tarfile.open(path, "r:gz") as archive:
        member = archive.extractfile("manifest.json")
        if member is None:
            raise RuntimeError("Static Library export has no manifest")
        payload = json.loads(member.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Static Library export manifest must be an object")
    return payload


def run_export(
    *,
    repository: LibrarySiteExportRepository,
    storage: ExportStorage,
    destination: Path,
    should_stop: Any = lambda: False,
) -> dict[str, Any]:
    """Read, validate, and atomically publish one static-site bundle."""
    candidates, aliases = repository.load_snapshot()
    export = build_library_export(
        candidates,
        aliases=aliases,
        storage=storage,
        should_stop=should_stop,
    )
    if should_stop():
        raise ExportStopped("Static Library export stopped before publication")
    bundle = write_export_bundle(export, destination=destination)
    manifest = _manifest(bundle)
    summary = {
        "kind": "library.site_export_summary",
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "revision": str(manifest.get("revision") or ""),
        "bundle_path": str(bundle),
        "bundle_sha256": _file_sha256(bundle),
        "documents_published": len(export.documents),
        "documents_excluded": sum(export.exclusions.values()),
        "entities": len(export.entities),
        "collections": len(export.collections),
        "classifications": len(export.classifications),
        "documents_with_previews": sum("preview" in row for row in export.documents),
        "exclusion_reasons": export.exclusions,
        "stopped": False,
    }
    print(
        f"static library export: {json.dumps(summary, ensure_ascii=False, sort_keys=True)}",
        flush=True,
    )
    return summary


def main() -> int:
    run_id = _run_id()
    settings = load_settings()
    document_storage = load_document_storage_settings(load_runtime_config())
    repository = LibrarySiteExportRepository(
        settings.database_url,
        schema=settings.database_schema,
    )
    destination = flow_artifacts_dir("library") / "site-exports" / f"run-{run_id}"
    storage = ExportStorage(
        endpoint_url=document_storage.primary.endpoint_url,
        public_document_bucket=document_storage.public_bucket,
        public_preview_bucket=document_storage.preview_bucket,
        public_content_bucket=document_storage.content_bucket,
    )
    stop_state = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_state["requested"] = True
        print(
            "static library export: graceful stop requested; no partial bundle will be published",
            flush=True,
        )

    signal.signal(signal.SIGINT, request_stop)
    try:
        try:
            summary = run_export(
                repository=repository,
                storage=storage,
                destination=destination,
                should_stop=lambda: bool(stop_state["requested"]),
            )
        except ExportStopped:
            summary = {
                "kind": "library.site_export_summary",
                "format": EXPORT_FORMAT,
                "version": EXPORT_VERSION,
                "documents_published": 0,
                "documents_excluded": 0,
                "stopped": True,
            }
        emit_run_artifact(summary)
        return 0
    finally:
        repository.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
