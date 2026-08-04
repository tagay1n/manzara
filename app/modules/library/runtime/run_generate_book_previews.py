"""Generate public WebP previews for applicable Library PDFs."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
from typing import Any, Mapping


def _bootstrap_repo_root() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)


_bootstrap_repo_root()

from boto3 import Session  # noqa: E402
from botocore.config import Config  # noqa: E402

from app.db import Database  # noqa: E402
from app.document_storage import (  # noqa: E402
    DEFAULT_S3_ENDPOINT,
    load_document_storage_settings,
)
from app.modules.library.preview_generation import (  # noqa: E402
    PreviewGenerationSettings,
    process_book,
)
from app.modules.library.preview_repository import LibraryPreviewRepository  # noqa: E402
from app.modules.library.previews import PREVIEW_RECIPE_VERSION  # noqa: E402
from app.run_artifact_channel import emit_run_artifact  # noqa: E402
from app.runtime_config import load_runtime_config  # noqa: E402
from app.settings import load_settings  # noqa: E402


TASK_ID = "library.generate_book_previews"
PANEL_ID = "library"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _required(mapping: Mapping[str, Any], key: str, path: str) -> str:
    value = str(mapping.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required config value: {path}.{key}")
    return value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Library PDF previews")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional candidate limit for a controlled smoke run",
    )
    return parser.parse_args()


def _run_id() -> int:
    value = str(os.environ.get("MANZARA_TASK_RUN_ID") or "").strip()
    if not value.isdigit() or int(value) <= 0:
        raise RuntimeError("MANZARA_TASK_RUN_ID is required")
    return int(value)


def _resolved_settings(payload: Mapping[str, Any], *, run_id: int) -> tuple[PreviewGenerationSettings, dict[str, str]]:
    documents = _mapping(payload.get("documents"))
    document_storage = load_document_storage_settings(payload)
    yandex = _mapping(payload.get("yandex"))
    cloud = _mapping(yandex.get("cloud"))
    buckets = _mapping(cloud.get("bucket"))
    artifacts_root = Path(
        os.environ.get("MANZARA_ARTIFACTS_ROOT", "~/.manzara")
    ).expanduser()
    settings = PreviewGenerationSettings(
        source_bucket=document_storage.public_bucket,
        target_bucket=_required(buckets, "book_previews", "yandex.cloud.bucket"),
        cache_dir=Path(
            _required(documents, "cache_path", "documents")
        ).expanduser(),
        workspace=artifacts_root / "library" / "book-previews" / f"run-{run_id}",
        source_endpoint_url=document_storage.primary.endpoint_url,
        source_region_name=document_storage.primary.region_name,
        encryption_key=_required(payload, "encryption_key", "config"),
    )
    credentials = {
        "source_access_key_id": document_storage.primary.access_key_id,
        "source_secret_access_key": document_storage.primary.secret_access_key,
        "target_access_key_id": _required(
            cloud, "aws_access_key_id", "yandex.cloud"
        ),
        "target_secret_access_key": _required(
            cloud, "aws_secret_access_key", "yandex.cloud"
        ),
        "target_endpoint_url": str(
            cloud.get("endpoint_url") or DEFAULT_S3_ENDPOINT
        ),
        "target_region_name": str(cloud.get("region_name") or "ru-central1"),
    }
    return settings, credentials


def _progress_payload(current: int, total: int, counters: Mapping[str, int]) -> dict[str, Any]:
    percent = 100 if total == 0 else round((current / total) * 100, 2)
    return {
        "current": int(current),
        "total": int(total),
        "percent": percent,
        "ready": int(counters.get("ready") or 0),
        "partial": int(counters.get("partial") or 0),
        "failed": int(counters.get("failed") or 0),
        "uploaded_objects": int(counters.get("uploaded_objects") or 0),
        "reused_objects": int(counters.get("reused_objects") or 0),
        "downloaded_sources": int(counters.get("downloaded_sources") or 0),
    }


def _publish_progress(db: Database, run_id: int, progress: dict[str, Any]) -> None:
    db.update_run_progress(run_id, progress)
    db.insert_event(
        "task.progress",
        task_id=TASK_ID,
        run_id=run_id,
        panel_id=PANEL_ID,
        payload={"status": "running", "progress": progress},
    )


def run_generation(
    *,
    repository: LibraryPreviewRepository,
    db: Database,
    source_s3: Any,
    target_s3: Any,
    settings: PreviewGenerationSettings,
    run_id: int,
    should_stop: Any,
    limit: int | None = None,
) -> dict[str, Any]:
    """Process candidates serially and return the structured run artifact."""
    candidates = repository.list_candidates(recipe_version=PREVIEW_RECIPE_VERSION)
    if limit is not None:
        candidates = candidates[: max(0, int(limit))]
    counters = {
        "ready": 0,
        "partial": 0,
        "failed": 0,
        "uploaded_objects": 0,
        "reused_objects": 0,
        "downloaded_sources": 0,
    }
    total = len(candidates)
    _publish_progress(db, run_id, _progress_payload(0, total, counters))
    print(
        f"library previews: start run_id={run_id} recipe={PREVIEW_RECIPE_VERSION} "
        f"candidates={total} source_bucket={settings.source_bucket} "
        f"target_bucket={settings.target_bucket}",
        flush=True,
    )

    processed = 0
    for candidate in candidates:
        result = process_book(
            candidate,
            repository=repository,
            settings=settings,
            source_s3=source_s3,
            target_s3=target_s3,
            run_id=run_id,
            log=lambda message: print(message, flush=True),
        )
        counters[result.status] = int(counters.get(result.status) or 0) + 1
        counters["uploaded_objects"] += result.uploaded_objects
        counters["reused_objects"] += result.reused_objects
        counters["downloaded_sources"] += int(result.downloaded_source)
        processed += 1
        _publish_progress(db, run_id, _progress_payload(processed, total, counters))
        if should_stop():
            print(
                "library previews: graceful stop boundary reached after current document",
                flush=True,
            )
            break

    summary = {
        "kind": "library.book_preview_summary",
        "recipe_version": PREVIEW_RECIPE_VERSION,
        "processed": processed,
        "total": total,
        **counters,
        "stopped": bool(should_stop()),
    }
    print(
        f"library previews: final {json.dumps(summary, ensure_ascii=False, sort_keys=True)}",
        flush=True,
    )
    return summary


def main() -> int:
    args = _parse_args()
    run_id = _run_id()
    app_settings = load_settings()
    preview_settings, credentials = _resolved_settings(load_runtime_config(), run_id=run_id)
    preview_settings.workspace.mkdir(parents=True, exist_ok=True)
    repository = LibraryPreviewRepository(
        app_settings.database_url,
        schema=app_settings.database_schema,
    )
    db = Database(app_settings.database_url, schema=app_settings.database_schema)
    source_s3 = Session().client(
        "s3",
        aws_access_key_id=credentials["source_access_key_id"],
        aws_secret_access_key=credentials["source_secret_access_key"],
        endpoint_url=preview_settings.source_endpoint_url,
        region_name=preview_settings.source_region_name,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    target_s3 = Session().client(
        "s3",
        aws_access_key_id=credentials["target_access_key_id"],
        aws_secret_access_key=credentials["target_secret_access_key"],
        endpoint_url=credentials["target_endpoint_url"],
        region_name=credentials["target_region_name"],
    )
    source_s3.head_bucket(Bucket=preview_settings.source_bucket)
    target_s3.head_bucket(Bucket=preview_settings.target_bucket)

    stop_state = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_state["requested"] = True
        print(
            "library previews: graceful stop requested; finishing current document",
            flush=True,
        )

    signal.signal(signal.SIGINT, request_stop)
    try:
        summary = run_generation(
            repository=repository,
            db=db,
            source_s3=source_s3,
            target_s3=target_s3,
            settings=preview_settings,
            run_id=run_id,
            should_stop=lambda: bool(stop_state["requested"]),
            limit=args.limit,
        )
        emit_run_artifact(summary)
        return 0
    finally:
        repository.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
