"""Task entry point for sequential legacy PDF content migration."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import sys
from typing import Any


def _bootstrap_repo_root() -> None:
    root = Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_bootstrap_repo_root()

import requests  # noqa: E402
from boto3 import Session  # noqa: E402
from botocore.config import Config  # noqa: E402

from app.artifacts import flow_artifacts_dir  # noqa: E402
from app.db import Database  # noqa: E402
from app.document_storage import (  # noqa: E402
    DocumentStorageSettings,
    load_document_storage_settings,
)
from app.modules.maintenance.content_storage_migration import (  # noqa: E402
    run_content_storage_migration,
)
from app.modules.maintenance.content_storage_migration_repository import (  # noqa: E402
    ContentStorageMigrationRepository,
)
from app.run_artifact_channel import emit_run_artifact  # noqa: E402
from app.runtime_config import load_runtime_config  # noqa: E402
from app.settings import load_settings  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Move Yandex-backed PDF content to Backblaze sequentially."
    )
    parser.add_argument("--md5", default=None)
    parser.add_argument("--limit", type=int, default=None)
    return parser


def _run_id() -> int:
    value = str(os.environ.get("MANZARA_TASK_RUN_ID") or "").strip()
    if not value.isdigit() or int(value) <= 0:
        raise RuntimeError("MANZARA_TASK_RUN_ID is required")
    return int(value)


def _client(connection: Any) -> Any:
    return Session().client(
        "s3",
        aws_access_key_id=connection.access_key_id,
        aws_secret_access_key=connection.secret_access_key,
        endpoint_url=connection.endpoint_url,
        region_name=connection.region_name,
        config=Config(
            signature_version="s3v4",
            connect_timeout=10,
            read_timeout=120,
            retries={"mode": "standard", "total_max_attempts": 3},
            s3={"addressing_style": "path"},
        ),
    )


def _validate_settings(settings: DocumentStorageSettings) -> None:
    required = {
        "documents.primary_storage.bucket.content": settings.content_bucket,
        "documents.primary_storage.bucket.content_images": (
            settings.content_images_bucket
        ),
        "yandex.cloud.bucket.content": settings.legacy_content_bucket,
        "yandex.cloud.bucket.image": settings.legacy_content_images_bucket,
    }
    missing = [name for name, value in required.items() if not str(value).strip()]
    if missing:
        raise RuntimeError("Missing required config value: " + ", ".join(missing))


def _public_check(url: str) -> bool:
    try:
        response = requests.head(url, allow_redirects=True, timeout=(10, 30))
        return response.status_code == 200
    except requests.RequestException:
        return False


def main() -> int:
    args = build_parser().parse_args()
    if args.limit is not None and args.limit <= 0:
        raise ValueError("--limit must be a positive integer")
    if args.md5 is not None:
        args.md5 = str(args.md5).strip().lower()
        if len(args.md5) != 32 or any(c not in "0123456789abcdef" for c in args.md5):
            raise ValueError("--md5 must be 32 lowercase hexadecimal characters")
    run_id = _run_id()
    app_settings = load_settings()
    storage = load_document_storage_settings(load_runtime_config())
    _validate_settings(storage)
    state_db = Database(
        app_settings.database_url, schema=app_settings.database_schema
    )
    repository = ContentStorageMigrationRepository(
        app_settings.database_url,
        schema=app_settings.database_schema,
        legacy_endpoint=storage.legacy.endpoint_url,
        legacy_bucket=storage.legacy_content_bucket,
    )
    legacy_s3 = _client(storage.legacy)
    primary_s3 = _client(storage.primary)
    for client, buckets in (
        (
            legacy_s3,
            (storage.legacy_content_bucket, storage.legacy_content_images_bucket),
        ),
        (primary_s3, (storage.content_bucket, storage.content_images_bucket)),
    ):
        for bucket in buckets:
            client.head_bucket(Bucket=bucket)
    stop = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop["requested"] = True
        print(
            "content migration: graceful stop requested; finishing current document",
            flush=True,
        )

    signal.signal(signal.SIGINT, request_stop)
    workspace = (
        flow_artifacts_dir("maintenance") / "content-migration" / f"run-{run_id}"
    )
    try:
        summary = run_content_storage_migration(
            repository=repository,
            state_db=state_db,
            legacy_s3=legacy_s3,
            primary_s3=primary_s3,
            settings=storage,
            workspace=workspace,
            run_id=run_id,
            should_stop=lambda: bool(stop["requested"]),
            public_check=_public_check,
            md5=args.md5,
            limit=args.limit,
        )
        emit_run_artifact(summary)
        return 130 if summary["stopped"] else 0
    finally:
        repository.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
