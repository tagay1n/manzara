"""Synchronize Yandex Disk documents into primary S3 storage."""

from __future__ import annotations

import json
import os
import signal
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from boto3 import Session
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
) -> Iterable[dict[str, Any]]:
    stack = [str(root).rstrip("/")]
    while stack and not should_stop():
        current = stack.pop()
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


def _inventory(s3: Any, bucket: str) -> dict[str, dict[str, Any]]:
    if hasattr(s3, "inventory"):
        return dict(s3.inventory(bucket))
    paginator = s3.get_paginator("list_objects_v2")
    return {
        str(item["Key"]): {
            "ContentLength": int(item.get("Size") or 0),
            "ETag": _etag(item.get("ETag")),
        }
        for page in paginator.paginate(Bucket=bucket)
        for item in page.get("Contents", [])
        if item.get("Key")
    }


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
    percent = round((current / total) * 100, 2) if total else (100 if stage == "completed" else 0)
    return {
        "stage": stage,
        "current": int(current),
        "total": int(total),
        "percent": percent,
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
) -> tuple[str, str] | None:
    if not existing or not existing.get("document_url"):
        return None
    return parse_object_url(
        _decrypt_url(str(existing["document_url"]), settings), settings.endpoint_url
    )


def _target_key(
    md5: str,
    resource: Mapping[str, Any],
    existing: Mapping[str, Any] | None,
    settings: DocumentStorageSettings,
) -> str:
    location = _existing_object_location(existing, settings)
    if location:
        existing_name = Path(location[1]).name
        if "/" not in location[1] and existing_name.lower().startswith(md5.lower() + "."):
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
    s3: Any,
    settings: DocumentStorageSettings,
    workspace: Path,
) -> tuple[Path, str]:
    if cache_file:
        return cache_file, "cache"
    if existing and existing.get("document_url"):
        location = parse_object_url(
            _decrypt_url(str(existing["document_url"]), settings), settings.endpoint_url
        )
        if location and location != target_location:
            candidate = workspace / f"source-{resource['source_md5']}.bin"
            try:
                _download_s3(s3, location[0], location[1], candidate)
                if calculate_md5(candidate) == resource["source_md5"]:
                    return candidate, "s3"
            except Exception:
                candidate.unlink(missing_ok=True)
    candidate = workspace / f"source-{resource['source_md5']}.bin"
    candidate.unlink(missing_ok=True)
    yadisk.download(resource["source_path"], str(candidate))
    if calculate_md5(candidate) != resource["source_md5"]:
        candidate.unlink(missing_ok=True)
        raise RuntimeError("Yandex Disk download MD5 mismatch")
    return candidate, "yandex"


def _publish_yandex_metadata(yadisk: Any, resource: dict[str, Any]) -> None:
    if resource.get("public_key") and resource.get("public_url"):
        return
    if not hasattr(yadisk, "publish"):
        return
    yadisk.publish(resource["source_path"])
    meta = yadisk.get_meta(
        resource["source_path"], fields=["public_key", "public_url"]
    )
    resource["public_key"] = str(_resource_value(meta, "public_key", "") or "")
    resource["public_url"] = str(_resource_value(meta, "public_url", "") or "")


def _verify_upload(s3: Any, bucket: str, key: str, md5: str, size: int) -> dict[str, Any]:
    head = dict(s3.head_object(Bucket=bucket, Key=key))
    metadata = head.get("Metadata") if isinstance(head.get("Metadata"), Mapping) else {}
    if int(head.get("ContentLength") or -1) != int(size):
        raise RuntimeError("S3 size verification failed")
    if str(metadata.get("source-md5") or "").lower() != md5:
        raise RuntimeError("S3 MD5 metadata verification failed")
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


def run_document_sync(
    *,
    repository: Any,
    state_db: Any,
    yadisk: Any,
    s3: Any,
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
        "source_s3": 0,
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
    resources = list(_walk_files(yadisk, settings.source_path, should_stop=should_stop))
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
    public_inventory = _inventory(s3, settings.public_bucket)
    private_inventory = _inventory(s3, settings.private_bucket)
    try:
        upstream_metadata = repository.list_upstream_metadata(
            s3, settings.upstream_bucket, settings.endpoint_url
        )
    except AttributeError:
        upstream_metadata = {}
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
                    settings.endpoint_url,
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
                        s3=s3, bucket=bucket, key=key, md5=md5, workspace=workspace
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
                    s3=s3,
                    settings=settings,
                    workspace=workspace,
                )
                if source_file.parent == workspace:
                    temporary_paths.append(source_file)
                counters[f"source_{source_kind}"] += 1
                was_present = remote is not None
                s3.upload_file(
                    str(source_file),
                    bucket,
                    key,
                    ExtraArgs={
                        "Metadata": {"source-md5": md5},
                        "ContentType": str(resource.get("mime_type") or "application/octet-stream"),
                    },
                )
                verified_head = _verify_upload(
                    s3, bucket, key, md5, int(resource["source_size"])
                )
                inventory[key] = dict(verified_head)
                counters["reuploaded" if was_present else "uploaded"] += 1

            _publish_yandex_metadata(yadisk, resource)
            canonical_url = object_url(settings.endpoint_url, bucket, key)
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
            public_key = (
                legacy_location[1]
                if legacy_location and legacy_location[0] == settings.public_bucket
                else key
            )
            if restricted and public_key in public_inventory:
                s3.delete_object(Bucket=settings.public_bucket, Key=public_key)
                if not _public_object_removed(s3, settings.public_bucket, public_key):
                    raise RuntimeError("Legacy public object still exists after deletion")
                public_inventory.pop(public_key, None)
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
    s3 = Session().client(
        "s3",
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key,
        endpoint_url=settings.endpoint_url,
        region_name=settings.region_name,
    )
    s3.head_bucket(Bucket=settings.public_bucket)
    s3.head_bucket(Bucket=settings.private_bucket)
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
                s3=s3,
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
