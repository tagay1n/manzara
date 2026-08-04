"""Synchronize Yandex Disk documents into primary S3 storage."""

from __future__ import annotations

import json
import os
import signal
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from boto3 import Session
from botocore.config import Config
from botocore.exceptions import ClientError
from yadisk_client import YaDisk

from app.artifacts import flow_artifacts_dir
from app.db import Database
from app.document_storage import (
    DocumentStorageSettings,
    build_cache_index,
    calculate_md5,
    document_object_key,
    find_valid_cache_entry,
    load_document_storage_settings,
    object_url,
    parse_object_url,
)
from app.modules.runtime_shared_utils import decrypt, encrypt, prefix
from app.modules.maintenance.document_sync_repository import (
    PostgresDocumentSyncRepository,
)
from app.run_artifact_channel import emit_run_artifact
from app.runtime_config import load_runtime_config
from app.settings import load_settings


TASK_ID = "maintenance.sync_documents_s3"
PANEL_ID = "maintenance"


def _resource_value(resource: Any, key: str, default: Any = None) -> Any:
    if isinstance(resource, Mapping):
        return resource.get(key, default)
    try:
        return resource[key]
    except Exception:
        return getattr(resource, key, default)


def _walk_files(
    yadisk: Any,
    root: str,
    *,
    should_stop: Callable[[], bool],
    on_directory: Callable[[str], None] | None = None,
) -> Iterable[dict[str, Any]]:
    stack = [str(root).rstrip("/")]
    while stack and not should_stop():
        current = stack.pop()
        if on_directory is not None:
            on_directory(current)
        children = list(
            yadisk.listdir(
                current,
                fields=[
                    "name",
                    "path",
                    "type",
                    "size",
                    "md5",
                    "mime_type",
                    "resource_id",
                    "public_key",
                    "public_url",
                ],
            )
        )
        for resource in reversed(children):
            resource_type = str(_resource_value(resource, "type", "") or "")
            path = str(_resource_value(resource, "path", "") or "").strip()
            if resource_type == "dir":
                if path:
                    stack.append(path)
                continue
            if resource_type != "file" or not path:
                continue
            yield {
                "source_path": path,
                "source_size": int(_resource_value(resource, "size", 0) or 0),
                "source_md5": str(_resource_value(resource, "md5", "") or "").lower(),
                "mime_type": _correct_mime(
                    str(_resource_value(resource, "mime_type", "") or ""), path
                ),
                "resource_id": str(_resource_value(resource, "resource_id", "") or ""),
                "public_key": str(_resource_value(resource, "public_key", "") or ""),
                "public_url": str(_resource_value(resource, "public_url", "") or ""),
            }


def _correct_mime(mime_type: str, source_path: str) -> str:
    normalized = str(mime_type or "").strip().lower()
    suffix = Path(str(source_path)).suffix.lower()
    if normalized == "application/octet-stream" and suffix == ".pdf":
        return "application/pdf"
    if normalized == "text/html" and suffix == ".txt":
        return "text/plain"
    if normalized == "text/html" and suffix == ".doc":
        return "text/plain"
    return normalized or "application/octet-stream"


def _etag(value: Any) -> str:
    return str(value or "").strip().strip('"')


def _inventory(
    s3: Any,
    bucket: str,
    *,
    should_stop: Callable[[], bool],
) -> dict[str, dict[str, Any]]:
    if hasattr(s3, "inventory"):
        return dict(s3.inventory(bucket))
    paginator = s3.get_paginator("list_objects_v2")
    result: dict[str, dict[str, Any]] = {}
    for page in paginator.paginate(Bucket=bucket):
        if should_stop():
            break
        for item in page.get("Contents", []):
            if item.get("Key"):
                result[str(item["Key"])] = {
                    "ContentLength": int(item.get("Size") or 0),
                    "ETag": _etag(item.get("ETag")),
                }
    return result


def _progress_payload(
    *,
    stage: str,
    current: int,
    total: int,
    bytes_completed: int,
    bytes_total: int,
    counters: Mapping[str, int],
    current_path: str = "",
) -> dict[str, Any]:
    if bytes_total > 0:
        percent = round((bytes_completed / bytes_total) * 100, 2)
    elif total:
        percent = round((current / total) * 100, 2)
    else:
        percent = 100 if stage == "completed" else 0
    return {
        "stage": stage,
        "current": int(current),
        "total": int(total),
        "percent": max(0, min(percent, 100)),
        "bytes_completed": int(bytes_completed),
        "bytes_total": int(bytes_total),
        "current_path": current_path,
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


def _download_s3(s3: Any, bucket: str, key: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    s3.download_file(bucket, key, str(target))
    return target


def _remote_is_verified(
    *,
    item: Mapping[str, Any],
    md5: str,
    size: int,
    cache_multipart_etag: str | None,
) -> bool:
    if int(item.get("ContentLength") or -1) != int(size):
        return False
    remote_etag = _etag(item.get("ETag"))
    if remote_etag == md5:
        return True
    return bool(cache_multipart_etag and cache_multipart_etag == remote_etag)


def _decrypt_url(value: str, settings: DocumentStorageSettings) -> str:
    if str(value or "").startswith(prefix):
        return decrypt(value, {"encryption_key": settings.encryption_key})
    return str(value or "")


def _store_url(
    value: str,
    *,
    restricted: bool,
    existing_value: Any,
    settings: DocumentStorageSettings,
) -> str:
    if not restricted:
        return value
    previous = str(existing_value or "")
    if previous.startswith(prefix) and _decrypt_url(previous, settings) == value:
        return previous
    return encrypt(value, {"encryption_key": settings.encryption_key})


def _document_needs_save(
    existing: Mapping[str, Any] | None,
    payload: Mapping[str, Any],
) -> bool:
    if existing is None:
        return True
    fields = (
        "mime_type",
        "ya_path",
        "ya_public_url",
        "ya_public_key",
        "ya_resource_id",
        "full",
        "sharing_restricted",
        "document_url",
        "primary_storage_size",
        "primary_storage_etag",
        "primary_storage_verified_at",
    )
    for field in fields:
        if existing.get(field) != payload.get(field):
            return True
    return bool(
        not existing.get("upstream_meta_url") and payload.get("upstream_meta_url")
    )


def _canonical_resource(resources: list[dict[str, Any]], existing: Mapping[str, Any] | None) -> dict[str, Any]:
    if existing:
        resource_id = str(existing.get("ya_resource_id") or "")
        source_path = str(existing.get("ya_path") or "")
        for resource in resources:
            if resource_id and resource["resource_id"] == resource_id:
                return resource
        for resource in resources:
            if source_path and resource["source_path"].removeprefix("disk:") == source_path:
                return resource
    return sorted(resources, key=lambda item: item["source_path"])[0]


def _is_restricted(path: str, settings: DocumentStorageSettings) -> bool:
    source = str(path).removeprefix("disk:").rstrip("/")
    restricted = str(settings.restricted_path).removeprefix("disk:").rstrip("/")
    return source == restricted or source.startswith(restricted + "/")


def _is_limited(path: str) -> bool:
    normalized = str(path).casefold()
    return "/limited/" in normalized and (
        "/milli_kitaphana/" in normalized or "/милли.китапханә/" in normalized
    )


def _existing_object_location(
    existing: Mapping[str, Any] | None,
    settings: DocumentStorageSettings,
) -> tuple[str, str, str] | None:
    if not existing or not existing.get("document_url"):
        return None
    url = _decrypt_url(str(existing["document_url"]), settings)
    for storage_name, endpoint_url in (
        ("primary", settings.primary.endpoint_url),
        ("legacy", settings.legacy.endpoint_url),
    ):
        location = parse_object_url(url, endpoint_url)
        if location:
            return storage_name, location[0], location[1]
    return None


def _target_key(
    md5: str,
    resource: Mapping[str, Any],
    existing: Mapping[str, Any] | None,
    settings: DocumentStorageSettings,
) -> str:
    location = _existing_object_location(existing, settings)
    if location:
        existing_name = Path(location[2]).name
        if "/" not in location[2] and existing_name.lower().startswith(md5.lower() + "."):
            return existing_name
    return document_object_key(md5, str(resource["source_path"]), resource.get("mime_type"))


def _verify_by_download(
    *,
    s3: Any,
    bucket: str,
    key: str,
    md5: str,
    workspace: Path,
) -> Path | None:
    candidate = workspace / f"verify-{md5}.bin"
    _download_s3(s3, bucket, key, candidate)
    if calculate_md5(candidate) == md5:
        return candidate
    candidate.unlink(missing_ok=True)
    return None


def _acquire_source(
    *,
    resource: Mapping[str, Any],
    existing: Mapping[str, Any] | None,
    target_location: tuple[str, str],
    cache_file: Path | None,
    yadisk: Any,
    primary_s3: Any,
    legacy_s3: Any,
    settings: DocumentStorageSettings,
    workspace: Path,
) -> tuple[Path, str]:
    if cache_file:
        return cache_file, "cache"
    location = _existing_object_location(existing, settings)
    if location:
        storage_name, bucket, key = location
        if storage_name != "primary" or (bucket, key) != target_location:
            source_s3 = primary_s3 if storage_name == "primary" else legacy_s3
            candidate = workspace / f"source-{resource['source_md5']}.bin"
            try:
                _download_s3(source_s3, bucket, key, candidate)
                if calculate_md5(candidate) == resource["source_md5"]:
                    return candidate, f"{storage_name}_s3"
            except Exception as exc:
                print(
                    f"document sync: legacy source unavailable "
                    f"storage={storage_name} bucket={bucket} key={key} "
                    f"error={type(exc).__name__}: {exc}; falling back to Yandex Disk",
                    flush=True,
                )
                candidate.unlink(missing_ok=True)
    candidate = workspace / f"source-{resource['source_md5']}.bin"
    candidate.unlink(missing_ok=True)
    yadisk.download(resource["source_path"], str(candidate))
    if calculate_md5(candidate) != resource["source_md5"]:
        candidate.unlink(missing_ok=True)
        raise RuntimeError("Yandex Disk download MD5 mismatch")
    return candidate, "yandex"


def _verify_upload(
    s3: Any,
    bucket: str,
    key: str,
    md5: str,
    size: int,
    workspace: Path,
) -> dict[str, Any]:
    head = dict(s3.head_object(Bucket=bucket, Key=key))
    metadata = head.get("Metadata") if isinstance(head.get("Metadata"), Mapping) else {}
    if int(head.get("ContentLength") or -1) != int(size):
        raise RuntimeError("S3 size verification failed")
    if str(metadata.get("source-md5") or "").lower() != md5:
        raise RuntimeError("S3 MD5 metadata verification failed")
    verified_path = _verify_by_download(
        s3=s3,
        bucket=bucket,
        key=key,
        md5=md5,
        workspace=workspace,
    )
    if verified_path is None:
        raise RuntimeError("S3 read-back MD5 verification failed")
    verified_path.unlink(missing_ok=True)
    return head


def _public_object_removed(s3: Any, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except KeyError:
        return True
    except ClientError as exc:
        error = exc.response.get("Error") if isinstance(exc.response, dict) else {}
        code = str(error.get("Code") or "") if isinstance(error, dict) else ""
        if code in {"404", "NoSuchKey", "NotFound"}:
            return True
        raise
    return False


def _abort_incomplete_uploads(s3: Any, bucket: str, key: str) -> int:
    """Remove unfinished multipart uploads for one content-addressed key."""
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
            s3.abort_multipart_upload(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
            )
            aborted += 1
            print(
                f"document sync: aborted incomplete multipart upload "
                f"bucket={bucket} key={key} upload_id={upload_id}",
                flush=True,
            )
        if not response.get("IsTruncated"):
            break
        key_marker = str(response.get("NextKeyMarker") or "") or None
        upload_id_marker = (
            str(response.get("NextUploadIdMarker") or "") or None
        )
        if not key_marker:
            break
    return aborted


def run_document_sync(
    *,
    repository: Any,
    state_db: Any,
    yadisk: Any,
    primary_s3: Any,
    legacy_s3: Any,
    settings: DocumentStorageSettings,
    workspace: Path,
    run_id: int,
    should_stop: Callable[[], bool],
) -> dict[str, Any]:
    """Discover and synchronize all documents with per-file safe boundaries."""
    workspace.mkdir(parents=True, exist_ok=True)
    counters = {
        "verified": 0,
        "uploaded": 0,
        "reuploaded": 0,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "duplicates": 0,
        "private_cleaned": 0,
        "failed": 0,
        "source_cache": 0,
        "source_primary_s3": 0,
        "source_legacy_s3": 0,
        "source_yandex": 0,
    }
    _publish_progress(
        state_db,
        run_id,
        _progress_payload(
            stage="discovering", current=0, total=0, bytes_completed=0,
            bytes_total=0, counters=counters,
        ),
    )
    directories_scanned = 0

    def report_directory(path: str) -> None:
        nonlocal directories_scanned
        directories_scanned += 1
        if directories_scanned == 1 or directories_scanned % 25 == 0:
            print(
                f"document sync: discovery progress directories={directories_scanned} "
                f"current_path={path}",
                flush=True,
            )

    print(
        f"document sync: discovery start source_path={settings.source_path}",
        flush=True,
    )
    resources = list(
        _walk_files(
            yadisk,
            settings.source_path,
            should_stop=should_stop,
            on_directory=report_directory,
        )
    )
    print(
        f"document sync: discovery complete files={len(resources)} "
        f"directories={directories_scanned}",
        flush=True,
    )
    cache_index = build_cache_index(settings.cache_path)
    existing_documents = repository.list_documents()
    grouped: dict[str, list[dict[str, Any]]] = {}
    missing_md5: list[dict[str, Any]] = []
    for resource in resources:
        if resource["source_md5"]:
            grouped.setdefault(resource["source_md5"], []).append(resource)
        else:
            missing_md5.append(resource)
    counters["duplicates"] = sum(max(0, len(items) - 1) for items in grouped.values())
    canonical = [
        _canonical_resource(items, existing_documents.get(md5))
        for md5, items in sorted(grouped.items())
    ] + missing_md5
    public_inventory: dict[str, dict[str, Any]] = {}
    private_inventory: dict[str, dict[str, Any]] = {}
    upstream_metadata: dict[str, str] = {}
    if not should_stop():
        print(
            f"document sync: inventory start bucket={settings.public_bucket}",
            flush=True,
        )
        public_inventory = _inventory(
            primary_s3,
            settings.public_bucket,
            should_stop=should_stop,
        )
    if not should_stop():
        print(
            f"document sync: inventory start bucket={settings.private_bucket}",
            flush=True,
        )
        private_inventory = _inventory(
            primary_s3,
            settings.private_bucket,
            should_stop=should_stop,
        )
    if not should_stop():
        try:
            upstream_metadata = repository.list_upstream_metadata(
                legacy_s3,
                settings.upstream_bucket,
                settings.legacy.endpoint_url,
            )
        except AttributeError:
            upstream_metadata = {}
    print(
        f"document sync: inventory complete public={len(public_inventory)} "
        f"private={len(private_inventory)} upstream={len(upstream_metadata)}",
        flush=True,
    )
    total = len(canonical)
    bytes_total = sum(int(item.get("source_size") or 0) for item in canonical)
    bytes_completed = processed = 0
    stopped = bool(should_stop())

    for resource in canonical:
        if stopped or should_stop():
            stopped = True
            break
        source_path = str(resource["source_path"])
        temporary_paths: list[Path] = []
        try:
            if not resource["source_md5"]:
                candidate = workspace / f"discover-{processed}.bin"
                yadisk.download(source_path, str(candidate))
                temporary_paths.append(candidate)
                resource["source_md5"] = calculate_md5(candidate)
                resource["source_size"] = candidate.stat().st_size
            md5 = str(resource["source_md5"])
            existing = existing_documents.get(md5)
            restricted = _is_restricted(source_path, settings)
            bucket = settings.private_bucket if restricted else settings.public_bucket
            key = _target_key(md5, resource, existing, settings)
            target_location = (bucket, key)
            inventory = private_inventory if restricted else public_inventory
            remote = inventory.get(key)
            cache_file: Path | None = None
            cache_multipart_etag: str | None = None
            verified_head: Mapping[str, Any] | None = None

            marker_matches = bool(
                existing
                and remote
                and existing.get("primary_storage_verified_at")
                and int(existing.get("primary_storage_size") or -1)
                == int(remote.get("ContentLength") or -2)
                and _etag(existing.get("primary_storage_etag")) == _etag(remote.get("ETag"))
                and parse_object_url(
                    _decrypt_url(str(existing.get("document_url") or ""), settings),
                    settings.primary.endpoint_url,
                ) == target_location
            )
            plain_etag_matches = bool(
                remote
                and int(remote.get("ContentLength") or -1) == int(resource["source_size"])
                and _etag(remote.get("ETag")) == md5
            )
            if marker_matches or plain_etag_matches:
                verified_head = remote
                counters["verified"] += 1
            elif remote:
                cache_entry = find_valid_cache_entry(cache_index, md5)
                if cache_entry:
                    cache_file, cache_multipart_etag = cache_entry
                if _remote_is_verified(
                    item=remote,
                    md5=md5,
                    size=int(resource["source_size"]),
                    cache_multipart_etag=cache_multipart_etag,
                ):
                    verified_head = remote
                    counters["verified"] += 1
                else:
                    verified_path = _verify_by_download(
                        s3=primary_s3,
                        bucket=bucket,
                        key=key,
                        md5=md5,
                        workspace=workspace,
                    )
                    if verified_path:
                        temporary_paths.append(verified_path)
                        verified_head = remote
                        counters["verified"] += 1

            if verified_head is None:
                if cache_file is None:
                    cache_entry = find_valid_cache_entry(cache_index, md5)
                    if cache_entry:
                        cache_file, cache_multipart_etag = cache_entry
                source_file, source_kind = _acquire_source(
                    resource=resource,
                    existing=existing,
                    target_location=target_location,
                    cache_file=cache_file,
                    yadisk=yadisk,
                    primary_s3=primary_s3,
                    legacy_s3=legacy_s3,
                    settings=settings,
                    workspace=workspace,
                )
                if source_file.parent == workspace:
                    temporary_paths.append(source_file)
                counters[f"source_{source_kind}"] += 1
                was_present = remote is not None
                _abort_incomplete_uploads(primary_s3, bucket, key)
                uploaded_bytes = 0
                progress_lock = threading.Lock()

                def publish_upload_progress(delta: int) -> None:
                    nonlocal uploaded_bytes
                    with progress_lock:
                        uploaded_bytes += max(0, int(delta))
                        _publish_progress(
                            state_db,
                            run_id,
                            _progress_payload(
                                stage="transferring",
                                current=processed,
                                total=total,
                                bytes_completed=(
                                    bytes_completed
                                    + min(
                                        uploaded_bytes,
                                        int(resource["source_size"]),
                                    )
                                ),
                                bytes_total=bytes_total,
                                counters=counters,
                                current_path=source_path,
                            ),
                        )

                primary_s3.upload_file(
                    str(source_file),
                    bucket,
                    key,
                    ExtraArgs={
                        "Metadata": {"source-md5": md5},
                        "ContentType": str(resource.get("mime_type") or "application/octet-stream"),
                    },
                    Callback=publish_upload_progress,
                )
                verified_head = _verify_upload(
                    primary_s3,
                    bucket,
                    key,
                    md5,
                    int(resource["source_size"]),
                    workspace,
                )
                inventory[key] = dict(verified_head)
                counters["reuploaded" if was_present else "uploaded"] += 1

            canonical_url = object_url(settings.primary.endpoint_url, bucket, key)
            stored_document_url = _store_url(
                canonical_url,
                restricted=restricted,
                existing_value=existing.get("document_url") if existing else None,
                settings=settings,
            )
            raw_public_url = str(resource.get("public_url") or "") or None
            public_url = (
                _store_url(
                    raw_public_url,
                    restricted=restricted,
                    existing_value=existing.get("ya_public_url") if existing else None,
                    settings=settings,
                )
                if raw_public_url
                else None
            )
            remote_size = int(verified_head.get("ContentLength") or resource["source_size"])
            remote_etag = _etag(verified_head.get("ETag"))
            existing_verification_matches = bool(
                existing
                and existing.get("primary_storage_verified_at")
                and int(existing.get("primary_storage_size") or -1) == remote_size
                and _etag(existing.get("primary_storage_etag")) == remote_etag
            )
            verified_at = (
                existing.get("primary_storage_verified_at")
                if existing_verification_matches
                else datetime.now(timezone.utc).isoformat()
            )
            created = existing is None
            document_payload = {
                "md5": md5,
                "mime_type": resource.get("mime_type"),
                "ya_path": source_path.removeprefix("disk:"),
                "ya_public_url": public_url,
                "ya_public_key": resource.get("public_key") or None,
                "ya_resource_id": resource.get("resource_id") or None,
                "full": not _is_limited(source_path),
                "sharing_restricted": restricted,
                "document_url": stored_document_url,
                "upstream_meta_url": upstream_metadata.get(md5),
                "primary_storage_size": remote_size,
                "primary_storage_etag": remote_etag,
                "primary_storage_verified_at": verified_at,
                "created": created,
            }
            if _document_needs_save(existing, document_payload):
                repository.save_verified_document(document_payload)
                counters["created" if created else "updated"] += 1
                existing_documents[md5] = dict(document_payload)
            else:
                counters["unchanged"] += 1

            legacy_location = _existing_object_location(existing, settings)
            if restricted:
                cleanup_targets: list[tuple[Any, str, str]] = []
                if key in public_inventory:
                    cleanup_targets.append((primary_s3, settings.public_bucket, key))
                if legacy_location:
                    storage_name, legacy_bucket, legacy_key = legacy_location
                    if storage_name == "primary" and legacy_bucket == settings.public_bucket:
                        target = (primary_s3, legacy_bucket, legacy_key)
                        if target not in cleanup_targets:
                            cleanup_targets.append(target)
                    elif (
                        storage_name == "legacy"
                        and legacy_bucket == settings.legacy_public_bucket
                    ):
                        cleanup_targets.append((legacy_s3, legacy_bucket, legacy_key))
                for cleanup_s3, cleanup_bucket, cleanup_key in cleanup_targets:
                    cleanup_s3.delete_object(Bucket=cleanup_bucket, Key=cleanup_key)
                    if not _public_object_removed(
                        cleanup_s3, cleanup_bucket, cleanup_key
                    ):
                        raise RuntimeError(
                            "Legacy public object still exists after deletion"
                        )
                    if cleanup_s3 is primary_s3:
                        public_inventory.pop(cleanup_key, None)
                    counters["private_cleaned"] += 1
            bytes_completed += int(resource.get("source_size") or 0)
            print(
                f"document sync: success md5={md5} source_path={source_path} "
                f"target=s3://{bucket}/{key}",
                flush=True,
            )
        except Exception as exc:
            counters["failed"] += 1
            print(
                f"document sync: failed source_path={source_path} "
                f"error={type(exc).__name__}: {exc}",
                flush=True,
            )
        finally:
            for temporary_path in temporary_paths:
                temporary_path.unlink(missing_ok=True)
        processed += 1
        _publish_progress(
            state_db,
            run_id,
            _progress_payload(
                stage="transferring", current=processed, total=total,
                bytes_completed=bytes_completed, bytes_total=bytes_total,
                counters=counters, current_path=source_path,
            ),
        )

    stage = "stopped" if stopped else "completed"
    _publish_progress(
        state_db,
        run_id,
        _progress_payload(
            stage=stage, current=processed, total=total,
            bytes_completed=bytes_completed, bytes_total=bytes_total,
            counters=counters,
        ),
    )
    return {
        "kind": "maintenance.document_s3_sync_summary",
        "discovered": len(resources),
        "considered": total,
        "processed": processed,
        "bytes_processed": bytes_completed,
        "stopped": stopped,
        **counters,
    }


def _run_id() -> int:
    value = str(os.environ.get("MANZARA_TASK_RUN_ID") or "").strip()
    if not value.isdigit() or int(value) <= 0:
        raise RuntimeError("MANZARA_TASK_RUN_ID is required")
    return int(value)


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
    """Fail before discovery when primary bucket visibility is unsafe."""
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
    legacy_s3 = _create_s3_client(settings.legacy)
    _validate_primary_buckets(
        primary_s3,
        settings.public_bucket,
        settings.private_bucket,
    )
    legacy_s3.head_bucket(Bucket=settings.upstream_bucket)
    stop_state = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_state["requested"] = True
        print("document sync: graceful stop requested; finishing current file", flush=True)

    signal.signal(signal.SIGINT, request_stop)
    workspace_root = flow_artifacts_dir("maintenance") / "document-s3-sync"
    workspace_root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=f"run-{run_id}-", dir=workspace_root) as temp_dir:
            summary = run_document_sync(
                repository=repository,
                state_db=state_db,
                yadisk=yadisk,
                primary_s3=primary_s3,
                legacy_s3=legacy_s3,
                settings=settings,
                workspace=Path(temp_dir),
                run_id=run_id,
                should_stop=lambda: bool(stop_state["requested"]),
            )
        print(f"document sync: final {json.dumps(summary, sort_keys=True)}", flush=True)
        emit_run_artifact(summary)
        return 1 if int(summary.get("failed") or 0) > 0 else 0
    finally:
        repository.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
