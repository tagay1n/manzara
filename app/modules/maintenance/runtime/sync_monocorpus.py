"""Guarded Yandex catalog synchronization and cleanup-plan execution."""

from __future__ import annotations

import json
import os
import signal
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable, Mapping

from boto3 import Session
from botocore.config import Config
from botocore.exceptions import ClientError
from yadisk_client import YaDisk
from yadisk.exceptions import PathExistsError, PathNotFoundError

from app.db import Database
from app.document_storage import (
    DocumentStorageSettings,
    load_document_storage_settings,
)
from app.modules.maintenance.document_cleanup_executor import execute_yandex_cleanup
from app.document_sync_filter import classify_document, normalize_document_mime
from app.modules.maintenance.monocorpus_sync_repository import MonocorpusSyncRepository
from app.run_artifact_channel import emit_run_artifact
from app.runtime_config import load_runtime_config
from app.settings import load_settings


TASK_ID = "maintenance.monocorpus_sync"
PANEL_ID = "maintenance"


def _resource_value(resource: Any, key: str, default: Any = None) -> Any:
    if isinstance(resource, Mapping):
        return resource.get(key, default)
    try:
        return resource[key]
    except Exception:
        return getattr(resource, key, default)


def _correct_mime(mime_type: str, source_path: str) -> str:
    return normalize_document_mime(source_path, mime_type)


def _is_restricted(path: str, settings: DocumentStorageSettings) -> bool:
    source = str(path).removeprefix("disk:").rstrip("/")
    restricted = str(settings.restricted_path).removeprefix("disk:").rstrip("/")
    return source == restricted or source.startswith(restricted + "/")


def _is_limited(path: str) -> bool:
    normalized = str(path).casefold()
    return "/limited/" in normalized and (
        "/milli_kitaphana/" in normalized or "/милли.китапханә/" in normalized
    )


def _walk_files(
    yadisk: Any,
    root: str,
    *,
    should_stop: Callable[[], bool],
) -> Iterable[dict[str, Any]]:
    stack = [str(root).rstrip("/")]
    while stack and not should_stop():
        current = stack.pop()
        try:
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
        except PathNotFoundError as exc:
            print(
                f"monocorpus sync: warning skipped unavailable Yandex path={current} "
                f"error={type(exc).__name__}: {exc}",
                flush=True,
            )
            continue
        for resource in reversed(children):
            resource_type = str(_resource_value(resource, "type", "") or "")
            path = str(_resource_value(resource, "path", "") or "")
            if resource_type == "dir":
                if path:
                    stack.append(path)
                continue
            if resource_type != "file" or not path:
                continue
            yield {
                "source_path": path,
                "source_size": int(_resource_value(resource, "size", 0) or 0),
                "source_md5": str(
                    _resource_value(resource, "md5", "") or ""
                ).lower(),
                "mime_type": _correct_mime(
                    str(_resource_value(resource, "mime_type", "") or ""), path
                ),
                "resource_id": str(
                    _resource_value(resource, "resource_id", "") or ""
                ),
                "public_key": str(
                    _resource_value(resource, "public_key", "") or ""
                ),
                "public_url": str(
                    _resource_value(resource, "public_url", "") or ""
                ),
            }


def _run_id() -> int:
    value = str(os.environ.get("MANZARA_TASK_RUN_ID") or "").strip()
    if not value.isdigit() or int(value) <= 0:
        raise RuntimeError("MANZARA_TASK_RUN_ID is required")
    return int(value)


def _s3_client(connection: Any) -> Any:
    return Session().client(
        "s3",
        aws_access_key_id=connection.access_key_id,
        aws_secret_access_key=connection.secret_access_key,
        endpoint_url=connection.endpoint_url,
        region_name=connection.region_name,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _meta_or_none(yadisk: Any, path: str) -> Mapping[str, Any] | None:
    try:
        return yadisk.get_meta(path, fields=["path", "md5", "resource_id", "public_url", "public_key"])
    except PathNotFoundError:
        return None


def _ensure_yandex_directory(yadisk: Any, directory: str) -> None:
    current = ""
    for part in PurePosixPath(str(directory).removeprefix("disk:")).parts:
        if part == "/":
            continue
        current += "/" + part
        try:
            yadisk.mkdir(current)
        except PathExistsError:
            continue


def _verify_moved_target(yadisk: Any, item: Mapping[str, Any]) -> bool:
    target = str(item.get("target_path") or "")
    if not target:
        return False
    target_meta = _meta_or_none(yadisk, target)
    if target_meta is None:
        return False
    return str(_resource_value(target_meta, "md5", "") or "").lower() == str(
        item.get("md5") or ""
    ).lower()


def _catalog_changed(existing: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    keys = (
        "mime_type",
        "ya_path",
        "ya_public_url",
        "ya_public_key",
        "ya_resource_id",
        "full",
        "sharing_restricted",
    )
    return any(existing.get(key) != payload.get(key) for key in keys)


def _delete_prefix(
    s3: Any,
    bucket: str,
    md5: str,
    *,
    missing_bucket_ok: bool = False,
    missing_buckets: set[str] | None = None,
) -> int:
    if missing_buckets is not None and bucket in missing_buckets:
        return 0
    deleted = 0
    previous_keys: tuple[str, ...] = ()
    try:
        while True:
            page = s3.list_objects_v2(Bucket=bucket, Prefix=md5, MaxKeys=1000)
            keys = tuple(
                str(item.get("Key") or "")
                for item in page.get("Contents", [])
                if str(item.get("Key") or "")
            )
            if not keys:
                return deleted
            if keys == previous_keys:
                raise RuntimeError(
                    f"Managed S3 objects did not disappear after deletion: "
                    f"s3://{bucket}/{md5}*"
                )
            previous_keys = keys
            for key in keys:
                s3.delete_object(Bucket=bucket, Key=key)
                deleted += 1
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code") or "")
        if missing_bucket_ok and code == "NoSuchBucket":
            if missing_buckets is not None:
                missing_buckets.add(bucket)
            print(
                f"monocorpus sync: warning legacy bucket missing; cleanup skipped bucket={bucket}",
                flush=True,
            )
            return 0
        raise


def _managed_legacy_buckets(config: Mapping[str, Any]) -> list[str]:
    yandex = config.get("yandex") if isinstance(config.get("yandex"), Mapping) else {}
    cloud = yandex.get("cloud") if isinstance(yandex.get("cloud"), Mapping) else {}
    buckets = cloud.get("bucket") if isinstance(cloud.get("bucket"), Mapping) else {}
    return sorted({str(value) for value in buckets.values() if str(value or "").strip()})


def _cleanup_managed_storage(
    *,
    md5: str,
    primary_s3: Any,
    legacy_s3: Any,
    settings: DocumentStorageSettings,
    config: Mapping[str, Any],
    missing_legacy_buckets: set[str] | None = None,
) -> int:
    deleted = 0
    for bucket in {settings.public_bucket, settings.private_bucket}:
        deleted += _delete_prefix(primary_s3, bucket, md5)
    if settings.preview_bucket:
        deleted += _delete_prefix(
            primary_s3, settings.preview_bucket, f"{md5}/"
        )
    for bucket in _managed_legacy_buckets(config):
        deleted += _delete_prefix(
            legacy_s3,
            bucket,
            md5,
            missing_bucket_ok=True,
            missing_buckets=missing_legacy_buckets,
        )
    return deleted


def _apply_cleanup(
    item: Mapping[str, Any],
    *,
    repository: MonocorpusSyncRepository,
    yadisk: Any,
    primary_s3: Any,
    legacy_s3: Any,
    settings: DocumentStorageSettings,
    config: Mapping[str, Any],
    run_id: int,
    missing_legacy_buckets: set[str],
) -> tuple[int, str]:
    cleanup_id = int(item["cleanup_id"])
    try:
        repository.mark_cleanup_running(cleanup_id, run_id=run_id, phase="yandex")
        executable = {**dict(item), "status": "running"}
        if str(item["action"]) == "move":
            target = str(item.get("target_path") or "")
            target_verified = _verify_moved_target(yadisk, item)
            source_exists = _meta_or_none(yadisk, str(item["source_path"])) is not None
            if target_verified and source_exists:
                raise RuntimeError(
                    "Verified cleanup target and original source both exist; refusing "
                    "to choose a destructive resolution automatically"
                )
            if not target_verified:
                if not source_exists:
                    reason = "Cleanup source and verified target are both missing"
                    repository.mark_cleanup_canceled(cleanup_id, reason)
                    print(
                        f"monocorpus sync: cleanup canceled cleanup_id={cleanup_id} "
                        f"reason={reason}",
                        flush=True,
                    )
                    return 0, "canceled"
                _ensure_yandex_directory(yadisk, str(PurePosixPath(target).parent))
                execute_yandex_cleanup(executable, yadisk=yadisk)
            if not _verify_moved_target(yadisk, item):
                raise RuntimeError("Moved Yandex target failed MD5 verification")
            if _meta_or_none(yadisk, str(item["source_path"])) is not None:
                raise RuntimeError("Original Yandex source remains after verified move")
            repository.mark_cleanup_phase(cleanup_id, "storage_cleanup")
            print(
                f"monocorpus sync: storage cleanup start cleanup_id={cleanup_id} "
                f"md5={item['md5']}",
                flush=True,
            )
            removed_objects = _cleanup_managed_storage(
                md5=str(item["md5"]),
                primary_s3=primary_s3,
                legacy_s3=legacy_s3,
                settings=settings,
                config=config,
                missing_legacy_buckets=missing_legacy_buckets,
            )
            print(
                f"monocorpus sync: storage cleanup complete cleanup_id={cleanup_id} "
                f"md5={item['md5']} objects_removed={removed_objects}",
                flush=True,
            )
            repository.mark_cleanup_phase(cleanup_id, "database_cleanup")
            repository.delete_document_state(str(item["md5"]))
        else:
            if _meta_or_none(yadisk, str(item["source_path"])) is not None:
                execute_yandex_cleanup(executable, yadisk=yadisk)
            if _meta_or_none(yadisk, str(item["source_path"])) is not None:
                raise RuntimeError("Duplicate Yandex resource remains after removal")
            removed_objects = 0
        repository.mark_cleanup_completed(cleanup_id)
        print(
            f"monocorpus sync: cleanup success cleanup_id={cleanup_id} "
            f"action={item['action']} md5={item['md5']} objects_removed={removed_objects}",
            flush=True,
        )
        return removed_objects, "completed"
    except Exception as exc:
        repository.mark_cleanup_failed(cleanup_id, f"{type(exc).__name__}: {exc}")
        print(
            f"monocorpus sync: cleanup failed cleanup_id={cleanup_id} "
            f"error={type(exc).__name__}: {exc}",
            flush=True,
        )
        return 0, "failed"


def _publish_progress(
    db: Any,
    run_id: int,
    counters: Mapping[str, int],
    path: str = "",
    *,
    stage: str = "streaming",
    current: int | None = None,
    total: int | None = None,
) -> None:
    payload = {"stage": stage, "current_path": path, **dict(counters)}
    if current is not None:
        payload["current"] = int(current)
    if total is not None:
        payload["total"] = int(total)
    db.update_run_progress(run_id, payload)
    db.insert_event(
        "task.progress",
        task_id=TASK_ID,
        run_id=run_id,
        panel_id=PANEL_ID,
        payload={"status": "running", "progress": payload},
    )


def run_monocorpus_sync(
    *,
    repository: MonocorpusSyncRepository,
    db: Any,
    yadisk: Any,
    primary_s3: Any,
    legacy_s3: Any,
    settings: DocumentStorageSettings,
    config: Mapping[str, Any],
    run_id: int,
    should_stop: Callable[[], bool],
) -> dict[str, Any]:
    """Apply persisted plans, then stream Yandex entries into the catalog."""
    counters = {
        "discovered": 0,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "published": 0,
        "duplicate_resources_queued": 0,
        "filtered": 0,
        "cleanups_completed": 0,
        "cleanups_canceled": 0,
        "cleanups_failed": 0,
        "objects_removed": 0,
        "failed": 0,
    }
    missing_legacy_buckets: set[str] = set()
    print(f"monocorpus sync: start run_id={run_id}", flush=True)
    cleanup_items = repository.list_active_cleanup()
    cleanup_total = len(cleanup_items)
    if cleanup_total:
        print(f"monocorpus sync: cleanup queue total={cleanup_total}", flush=True)
    for cleanup_current, item in enumerate(cleanup_items, start=1):
        if should_stop():
            break
        _publish_progress(
            db,
            run_id,
            counters,
            str(item.get("source_path") or ""),
            stage="cleanup",
            current=cleanup_current - 1,
            total=cleanup_total,
        )
        removed, outcome = _apply_cleanup(
            item,
            repository=repository,
            yadisk=yadisk,
            primary_s3=primary_s3,
            legacy_s3=legacy_s3,
            settings=settings,
            config=config,
            run_id=run_id,
            missing_legacy_buckets=missing_legacy_buckets,
        )
        counters["objects_removed"] += removed
        counters[f"cleanups_{outcome}"] += 1
        counters["failed"] += int(outcome == "failed")
        _publish_progress(
            db,
            run_id,
            counters,
            str(item.get("source_path") or ""),
            stage="cleanup",
            current=cleanup_current,
            total=cleanup_total,
        )

    existing = repository.list_documents()
    seen: set[str] = set()
    for resource in _walk_files(yadisk, settings.source_path, should_stop=should_stop):
        if should_stop():
            break
        counters["discovered"] += 1
        md5 = str(resource.get("source_md5") or "").lower()
        source_path = str(resource.get("source_path") or "")
        decision = classify_document(source_path, str(resource.get("mime_type") or ""))
        if not decision.accepted:
            counters["filtered"] += 1
            print(
                f"monocorpus sync: filtered path={source_path} "
                f"mime_type={decision.mime_type} reason={decision.reason}",
                flush=True,
            )
            _publish_progress(db, run_id, counters, source_path)
            continue
        resource["mime_type"] = decision.mime_type
        if not md5:
            counters["failed"] += 1
            print(f"monocorpus sync: warning missing md5 path={source_path}", flush=True)
            continue
        current = existing.get(md5)
        if md5 in seen or (
            current
            and str(current.get("ya_path") or "").removeprefix("disk:")
            != source_path.removeprefix("disk:")
        ):
            canonical_exists = True
            if current and md5 not in seen:
                canonical_path = str(current.get("ya_path") or "")
                canonical_exists = _meta_or_none(yadisk, canonical_path) is not None
            if canonical_exists:
                cleanup_id, created = repository.enqueue_cleanup(
                    {
                        "scope": "duplicate_resource",
                        "action": "delete",
                        "reason": "duplicate_md5",
                        "md5": md5,
                        "source_resource_id": resource.get("resource_id") or source_path,
                        "source_path": source_path,
                        "target_path": None,
                        "evidence": {"canonical_path": current.get("ya_path") if current else None},
                    }
                )
                counters["duplicate_resources_queued"] += int(created)
                cleanup_item = {
                    "cleanup_id": cleanup_id,
                    "scope": "duplicate_resource",
                    "action": "delete",
                    "reason": "duplicate_md5",
                    "md5": md5,
                    "source_resource_id": resource.get("resource_id") or source_path,
                    "source_path": source_path,
                    "target_path": None,
                    "status": "planned",
                }
                _, outcome = _apply_cleanup(
                    cleanup_item,
                    repository=repository,
                    yadisk=yadisk,
                    primary_s3=primary_s3,
                    legacy_s3=legacy_s3,
                    settings=settings,
                    config=config,
                    run_id=run_id,
                    missing_legacy_buckets=missing_legacy_buckets,
                )
                counters[f"cleanups_{outcome}"] += 1
                counters["failed"] += int(outcome == "failed")
                continue
        restricted = _is_restricted(source_path, settings)
        public_url = str(
            resource.get("public_url") or (current or {}).get("ya_public_url") or ""
        ) or None
        public_key = str(
            resource.get("public_key") or (current or {}).get("ya_public_key") or ""
        ) or None
        if not restricted and not public_url:
            yadisk.publish(source_path)
            published = _meta_or_none(yadisk, source_path) or {}
            public_url = str(_resource_value(published, "public_url", "") or "") or None
            public_key = str(_resource_value(published, "public_key", "") or "") or None
            if not public_url:
                raise RuntimeError(f"Yandex publish did not return public URL: {source_path}")
            counters["published"] += 1
        payload = {
            "md5": md5,
            "mime_type": _correct_mime(str(resource.get("mime_type") or ""), source_path),
            "ya_path": source_path.removeprefix("disk:"),
            "ya_public_url": public_url,
            "ya_public_key": public_key,
            "ya_resource_id": resource.get("resource_id") or None,
            "full": not _is_limited(source_path),
            "sharing_restricted": restricted,
        }
        if current is None:
            repository.save_discovered_document(payload)
            counters["created"] += 1
        elif _catalog_changed(current, payload):
            repository.save_discovered_document(payload)
            counters["updated"] += 1
        else:
            counters["unchanged"] += 1
        existing[md5] = {**(current or {}), **payload}
        seen.add(md5)
        print(
            f"monocorpus sync: catalog success md5={md5} path={source_path} "
            f"state={'created' if current is None else 'updated' if _catalog_changed(current, payload) else 'unchanged'}",
            flush=True,
        )
        _publish_progress(db, run_id, counters, source_path)
    summary = {
        "kind": "maintenance.monocorpus_sync_summary",
        **counters,
        "stopped": bool(should_stop()),
    }
    print(f"monocorpus sync: final {json.dumps(summary, sort_keys=True)}", flush=True)
    return summary


def main() -> int:
    run_id = _run_id()
    app_settings = load_settings()
    config = load_runtime_config()
    settings = load_document_storage_settings(config)
    repository = MonocorpusSyncRepository(
        app_settings.database_url, schema=app_settings.database_schema
    )
    db = Database(app_settings.database_url, schema=app_settings.database_schema)
    yadisk = YaDisk(settings.yadisk_token)
    if yadisk.check_token() is False:
        raise RuntimeError("Yandex Disk token validation failed")
    primary_s3 = _s3_client(settings.primary)
    legacy_s3 = _s3_client(settings.legacy)
    stop_state = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_state["requested"] = True
        print("monocorpus sync: graceful stop requested; finishing current item", flush=True)

    signal.signal(signal.SIGINT, request_stop)
    try:
        summary = run_monocorpus_sync(
            repository=repository,
            db=db,
            yadisk=yadisk,
            primary_s3=primary_s3,
            legacy_s3=legacy_s3,
            settings=settings,
            config=config,
            run_id=run_id,
            should_stop=lambda: bool(stop_state["requested"]),
        )
        emit_run_artifact(summary)
        return 0
    finally:
        repository.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
