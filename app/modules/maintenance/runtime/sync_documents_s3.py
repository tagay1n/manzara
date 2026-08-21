"""Upload PostgreSQL-discovered documents into primary Backblaze storage."""

from __future__ import annotations

import json
import os
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from boto3 import Session
from botocore.config import Config
from botocore.exceptions import ClientError
from yadisk_client import YaDisk

from app.db import Database
from app.document_storage import (
    DocumentStorageSettings,
    build_cache_index,
    document_object_key,
    find_valid_cache_entry,
    load_document_storage_settings,
    materialize_cached_document,
    normalized_extension,
    object_url,
)
from app.modules.maintenance.document_sync_lock import document_sync_lock
from app.modules.maintenance.document_sync_repository import (
    PostgresDocumentSyncRepository,
)
from app.modules.runtime_shared_utils import encrypt
from app.run_artifact_channel import emit_run_artifact
from app.runtime_config import load_runtime_config
from app.settings import load_settings


TASK_ID = "maintenance.sync_documents_s3"
PANEL_ID = "maintenance"


class YandexDownloadUnavailable(RuntimeError):
    """A pending document could not be acquired from its persisted Yandex path."""


def _etag(value: Any) -> str:
    return str(value or "").strip().strip('"')


def _progress_payload(
    *,
    stage: str,
    current: int,
    total: int,
    counters: Mapping[str, int],
    current_path: str = "",
    current_bytes: int = 0,
    current_size: int = 0,
) -> dict[str, Any]:
    item_fraction = (
        min(max(current_bytes, 0), current_size) / current_size
        if current_size > 0
        else 0
    )
    completed = current + item_fraction if stage == "uploading" else current
    percent = round((completed / total) * 100, 2) if total else 100
    return {
        "stage": stage,
        "current": int(current),
        "total": int(total),
        "percent": max(0, min(percent, 100)),
        "current_path": current_path,
        "current_bytes": int(current_bytes),
        "current_size": int(current_size),
        **{key: int(value) for key, value in counters.items()},
    }


def _publish_progress(state_db: Any, run_id: int, payload: dict[str, Any]) -> None:
    state_db.update_run_progress(run_id, payload)
    state_db.insert_event(
        "task.progress",
        task_id=TASK_ID,
        run_id=run_id,
        panel_id=PANEL_ID,
        payload={"status": "running", "progress": payload},
    )


def _head_object_or_none(s3: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return dict(s3.head_object(Bucket=bucket, Key=key))
    except KeyError:
        return None
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def _remote_matches(
    remote: Mapping[str, Any] | None,
    *,
    md5: str,
    size: int,
) -> bool:
    if not remote or int(remote.get("ContentLength") or -1) != int(size):
        return False
    metadata = remote.get("Metadata")
    source_md5 = (
        str(metadata.get("source-md5") or "").lower()
        if isinstance(metadata, Mapping)
        else ""
    )
    return source_md5 == md5 or _etag(remote.get("ETag")).lower() == md5


def _confirm_upload(
    s3: Any,
    bucket: str,
    key: str,
    md5: str,
    size: int,
) -> dict[str, Any]:
    head = dict(s3.head_object(Bucket=bucket, Key=key))
    metadata = head.get("Metadata")
    if int(head.get("ContentLength") or -1) != int(size):
        raise RuntimeError("S3 size verification failed")
    if not isinstance(metadata, Mapping) or str(
        metadata.get("source-md5") or ""
    ).lower() != md5:
        raise RuntimeError("S3 MD5 metadata verification failed")
    return head


def _abort_incomplete_uploads(s3: Any, bucket: str, key: str) -> int:
    aborted = 0
    key_marker: str | None = None
    upload_id_marker: str | None = None
    while True:
        request: dict[str, Any] = {"Bucket": bucket, "Prefix": key}
        if key_marker:
            request["KeyMarker"] = key_marker
        if upload_id_marker:
            request["UploadIdMarker"] = upload_id_marker
        response = s3.list_multipart_uploads(**request)
        for upload in response.get("Uploads", []):
            upload_key = str(upload.get("Key") or "")
            upload_id = str(upload.get("UploadId") or "")
            if upload_key != key or not upload_id:
                continue
            s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
            aborted += 1
            print(
                "document upload: aborted incomplete multipart upload "
                f"bucket={bucket} key={key} upload_id={upload_id}",
                flush=True,
            )
        if not response.get("IsTruncated"):
            break
        key_marker = str(response.get("NextKeyMarker") or "") or None
        upload_id_marker = str(response.get("NextUploadIdMarker") or "") or None
        if not key_marker:
            break
    return aborted


def _public_object_removed(s3: Any, bucket: str, key: str) -> bool:
    return _head_object_or_none(s3, bucket, key) is None


def _acquire_source(
    *,
    row: Mapping[str, Any],
    cache_index: Mapping[str, list[Path]],
    yadisk: Any,
    settings: DocumentStorageSettings,
) -> tuple[Path, str]:
    md5 = str(row["md5"])
    cached = find_valid_cache_entry(cache_index, md5)
    if cached:
        return cached[0], "cache"
    source_path = str(row.get("ya_path") or "")
    if not source_path.strip():
        raise YandexDownloadUnavailable("persisted Yandex path is empty")

    def download(candidate: Path) -> None:
        try:
            yadisk.download(source_path, str(candidate))
        except Exception as exc:
            raise YandexDownloadUnavailable(
                f"{type(exc).__name__}: {exc}"
            ) from exc

    source = materialize_cached_document(
        cache_path=settings.cache_path,
        expected_md5=md5,
        extension=normalized_extension(source_path, row.get("mime_type")),
        download=download,
    )
    return source, "yandex"


def _stored_document_url(
    canonical_url: str,
    *,
    restricted: bool,
    settings: DocumentStorageSettings,
) -> str:
    if not restricted:
        return canonical_url
    return encrypt(canonical_url, {"encryption_key": settings.encryption_key})


def run_document_upload(
    *,
    repository: Any,
    state_db: Any,
    yadisk: Any,
    primary_s3: Any,
    settings: DocumentStorageSettings,
    run_id: int,
    should_stop: Callable[[], bool],
) -> dict[str, Any]:
    """Upload pending PostgreSQL rows at one-document safe boundaries."""
    pending = repository.list_pending_documents()
    total = len(pending)
    cache_index = build_cache_index(settings.cache_path)
    counters = {
        "uploaded": 0,
        "reuploaded": 0,
        "recovered_existing": 0,
        "checkpointed": 0,
        "checkpoint_raced": 0,
        "source_cache": 0,
        "source_yandex": 0,
        "skipped_download": 0,
        "private_cleaned": 0,
        "failed": 0,
        "bytes_uploaded": 0,
    }
    print(
        f"document upload: start run_id={run_id} pending={total} "
        f"cache_entries={len(cache_index)}",
        flush=True,
    )
    _publish_progress(
        state_db,
        run_id,
        _progress_payload(
            stage="uploading", current=0, total=total, counters=counters
        ),
    )
    processed = 0
    stopped = bool(should_stop())
    for row in pending:
        if stopped or should_stop():
            stopped = True
            break
        md5 = str(row["md5"])
        source_path = str(row.get("ya_path") or "")
        print(
            f"document upload: process current={processed + 1}/{total} "
            f"md5={md5} path={source_path}",
            flush=True,
        )
        try:
            source_file, source_kind = _acquire_source(
                row=row,
                cache_index=cache_index,
                yadisk=yadisk,
                settings=settings,
            )
            counters[f"source_{source_kind}"] += 1
            size = source_file.stat().st_size
            restricted = bool(row.get("sharing_restricted"))
            bucket = settings.private_bucket if restricted else settings.public_bucket
            key = document_object_key(md5, source_path, row.get("mime_type"))
            remote = _head_object_or_none(primary_s3, bucket, key)
            if _remote_matches(remote, md5=md5, size=size):
                verified_head = remote or {}
                counters["recovered_existing"] += 1
                print(
                    f"document upload: existing object verified md5={md5} "
                    f"target=s3://{bucket}/{key}",
                    flush=True,
                )
            else:
                was_present = remote is not None
                _abort_incomplete_uploads(primary_s3, bucket, key)
                uploaded_bytes = 0
                progress_lock = threading.Lock()

                def publish_upload_progress(delta: int) -> None:
                    nonlocal uploaded_bytes
                    with progress_lock:
                        increment = max(0, int(delta))
                        uploaded_bytes += increment
                        _publish_progress(
                            state_db,
                            run_id,
                            _progress_payload(
                                stage="uploading",
                                current=processed,
                                total=total,
                                counters=counters,
                                current_path=source_path,
                                current_bytes=uploaded_bytes,
                                current_size=size,
                            ),
                        )

                primary_s3.upload_file(
                    str(source_file),
                    bucket,
                    key,
                    ExtraArgs={
                        "Metadata": {"source-md5": md5},
                        "ContentType": str(
                            row.get("mime_type") or "application/octet-stream"
                        ),
                    },
                    Callback=publish_upload_progress,
                )
                verified_head = _confirm_upload(
                    primary_s3, bucket, key, md5, size
                )
                counters["uploaded"] += 1
                counters["reuploaded"] += int(was_present)
                counters["bytes_uploaded"] += size

            if restricted:
                public_remote = _head_object_or_none(
                    primary_s3, settings.public_bucket, key
                )
                if public_remote is not None:
                    primary_s3.delete_object(Bucket=settings.public_bucket, Key=key)
                    if not _public_object_removed(
                        primary_s3, settings.public_bucket, key
                    ):
                        raise RuntimeError(
                            "Obsolete public object remains after deletion"
                        )
                    counters["private_cleaned"] += 1

            canonical_url = object_url(
                settings.primary.endpoint_url, bucket, key
            )
            checkpoint = {
                "document_url": _stored_document_url(
                    canonical_url,
                    restricted=restricted,
                    settings=settings,
                ),
                "primary_storage_size": int(
                    verified_head.get("ContentLength") or size
                ),
                "primary_storage_etag": _etag(verified_head.get("ETag")),
                "primary_storage_verified_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }
            if repository.save_storage_checkpoint(md5, checkpoint):
                counters["checkpointed"] += 1
            else:
                counters["checkpoint_raced"] += 1
                print(
                    f"document upload: checkpoint skipped md5={md5} "
                    "reason=row no longer pending",
                    flush=True,
                )
            print(
                f"document upload: success md5={md5} target=s3://{bucket}/{key}",
                flush=True,
            )
        except YandexDownloadUnavailable as exc:
            counters["skipped_download"] += 1
            print(
                f"document upload: skipped md5={md5} path={source_path} "
                f"reason=yandex_unavailable error={exc}",
                flush=True,
            )
        except Exception as exc:
            counters["failed"] += 1
            print(
                f"document upload: failed md5={md5} path={source_path} "
                f"error={type(exc).__name__}: {exc}",
                flush=True,
            )
        processed += 1
        _publish_progress(
            state_db,
            run_id,
            _progress_payload(
                stage="uploading",
                current=processed,
                total=total,
                counters=counters,
                current_path=source_path,
            ),
        )
        if should_stop():
            stopped = True
            break

    pending_after = repository.count_pending_documents()
    summary = {
        "kind": "maintenance.document_s3_upload_summary",
        "pending_before": total,
        "pending_after": pending_after,
        "processed": processed,
        "stopped": stopped,
        **counters,
    }
    _publish_progress(
        state_db,
        run_id,
        _progress_payload(
            stage="stopped" if stopped else "completed",
            current=processed,
            total=total,
            counters=counters,
        ),
    )
    print(f"document upload: final {json.dumps(summary, sort_keys=True)}", flush=True)
    return summary


def _run_id() -> int:
    value = str(os.environ.get("MANZARA_TASK_RUN_ID") or "").strip()
    if not value.isdigit() or int(value) <= 0:
        raise RuntimeError("MANZARA_TASK_RUN_ID is required")
    return int(value)


def _result_exit_code(_summary: Mapping[str, Any]) -> int:
    """Per-item gaps are reportable outcomes, not process failures."""
    return 0


def _create_s3_client(connection: Any) -> Any:
    return Session().client(
        "s3",
        aws_access_key_id=connection.access_key_id,
        aws_secret_access_key=connection.secret_access_key,
        endpoint_url=connection.endpoint_url,
        region_name=connection.region_name,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )


def _allows_public_read(acl: Mapping[str, Any]) -> bool:
    for grant in acl.get("Grants", []):
        if not isinstance(grant, Mapping):
            continue
        grantee = grant.get("Grantee")
        if not isinstance(grantee, Mapping):
            continue
        if (
            str(grantee.get("URI") or "").endswith("/AllUsers")
            and str(grant.get("Permission") or "") in {"READ", "FULL_CONTROL"}
        ):
            return True
    return False


def _validate_primary_buckets(s3: Any, public_bucket: str, private_bucket: str) -> None:
    if public_bucket == private_bucket:
        raise RuntimeError("Document public and private buckets must be different")
    s3.head_bucket(Bucket=public_bucket)
    s3.head_bucket(Bucket=private_bucket)
    if not _allows_public_read(s3.get_bucket_acl(Bucket=public_bucket)):
        raise RuntimeError(
            f"Document public bucket must allow public read: {public_bucket}"
        )
    if _allows_public_read(s3.get_bucket_acl(Bucket=private_bucket)):
        raise RuntimeError(
            f"Document private bucket must not allow public read: {private_bucket}"
        )


def main() -> int:
    run_id = _run_id()
    app_settings = load_settings()
    settings = load_document_storage_settings(load_runtime_config())
    state_db = Database(app_settings.database_url, schema=app_settings.database_schema)
    repository = PostgresDocumentSyncRepository(
        app_settings.database_url, schema=app_settings.database_schema
    )
    yadisk = YaDisk(settings.yadisk_token)
    if yadisk.check_token() is False:
        raise RuntimeError("Yandex Disk token validation failed")
    primary_s3 = _create_s3_client(settings.primary)
    _validate_primary_buckets(
        primary_s3,
        settings.public_bucket,
        settings.private_bucket,
    )
    stop_state = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_state["requested"] = True
        print(
            "document upload: graceful stop requested; finishing current document",
            flush=True,
        )

    signal.signal(signal.SIGINT, request_stop)
    try:
        with document_sync_lock(
            app_settings.database_url, schema=app_settings.database_schema
        ):
            summary = run_document_upload(
                repository=repository,
                state_db=state_db,
                yadisk=yadisk,
                primary_s3=primary_s3,
                settings=settings,
                run_id=run_id,
                should_stop=lambda: bool(stop_state["requested"]),
            )
        emit_run_artifact(summary)
        return _result_exit_code(summary)
    finally:
        repository.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
