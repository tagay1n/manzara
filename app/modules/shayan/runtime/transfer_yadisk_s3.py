"""Move Shayan videos from Yandex Disk into S3-compatible object storage."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, Mapping

from boto3 import Session
from yadisk_client import YaDisk

from app.artifacts import flow_artifacts_dir
from app.db import Database
from app.run_artifact_channel import emit_run_artifact
from app.runtime_config import load_runtime_config
from app.settings import load_settings


TASK_ID = "shayan.transfer_yadisk_s3"
PANEL_ID = "shayan"
VIDEO_EXTENSIONS = {
    ".avi",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ts",
    ".webm",
}


@dataclass(frozen=True)
class S3Target:
    endpoint_url: str
    region_name: str
    bucket: str
    prefix: str
    access_key_id: str
    secret_access_key: str


@dataclass(frozen=True)
class TransferSettings:
    yadisk_token: str
    source_dirs: Dict[str, str]
    target: S3Target


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _required(mapping: Mapping[str, Any], key: str, path: str) -> str:
    value = str(mapping.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required config value: {path}.{key}")
    return value


def load_transfer_settings(payload: Mapping[str, Any]) -> TransferSettings:
    """Parse the one supported transfer configuration contract."""
    yandex = _mapping(payload.get("yandex"))
    disk = _mapping(yandex.get("disk"))
    shayan_sources = _mapping(disk.get("shayan"))
    storage = _mapping(payload.get("object_storage"))
    archive = _mapping(storage.get("shayan_archive"))
    if not archive:
        raise RuntimeError(
            "Missing required config section: object_storage.shayan_archive"
        )

    return TransferSettings(
        yadisk_token=_required(disk, "oauth_token", "yandex.disk"),
        source_dirs={
            "cartoons": _required(shayan_sources, "cartoons", "yandex.disk.shayan"),
            "shows": _required(shayan_sources, "shows", "yandex.disk.shayan"),
        },
        target=S3Target(
            endpoint_url=_required(
                archive, "endpoint_url", "object_storage.shayan_archive"
            ),
            region_name=_required(
                archive, "region_name", "object_storage.shayan_archive"
            ),
            bucket=_required(archive, "bucket", "object_storage.shayan_archive"),
            prefix=str(archive.get("prefix") or "").strip().strip("/"),
            access_key_id=_required(
                archive, "access_key_id", "object_storage.shayan_archive"
            ),
            secret_access_key=_required(
                archive,
                "secret_access_key",
                "object_storage.shayan_archive",
            ),
        ),
    )


def _plain_remote_path(path: str) -> str:
    value = str(path or "").strip()
    if value.startswith("disk:"):
        value = value[5:]
    return "/" + value.lstrip("/")


def destination_key(
    *,
    source_root: str,
    source_path: str,
    category: str,
    prefix: str,
) -> str:
    """Map one source path into a stable category-preserving S3 key."""
    root = PurePosixPath(_plain_remote_path(source_root))
    source = PurePosixPath(_plain_remote_path(source_path))
    try:
        relative = source.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(
            f"Source path {source_path!r} is outside root {source_root!r}"
        ) from exc
    parts = [str(prefix or "").strip("/"), str(category).strip("/"), relative]
    return posixpath.join(*(part for part in parts if part))


def _resource_value(resource: Any, key: str, default: Any = None) -> Any:
    if isinstance(resource, Mapping):
        return resource.get(key, default)
    try:
        return resource[key]
    except Exception:
        return getattr(resource, key, default)


def _is_video(resource: Any) -> bool:
    mime_type = str(_resource_value(resource, "mime_type", "") or "").lower()
    name = str(_resource_value(resource, "name", "") or "")
    return (
        mime_type.startswith("video/")
        or PurePosixPath(name).suffix.lower() in VIDEO_EXTENSIONS
    )


def _walk_videos(
    yadisk: Any,
    root: str,
    *,
    should_stop: Callable[[], bool],
) -> Iterable[Dict[str, Any]]:
    stack = [str(root).rstrip("/")]
    while stack:
        if should_stop():
            return
        current = stack.pop()
        children = list(
            yadisk.listdir(
                current,
                fields=["name", "path", "type", "size", "md5", "mime_type"],
            )
        )
        for resource in reversed(children):
            resource_type = str(_resource_value(resource, "type", "") or "")
            path = str(_resource_value(resource, "path", "") or "").strip()
            if resource_type == "dir":
                if path:
                    stack.append(path)
                continue
            if resource_type != "file" or not path or not _is_video(resource):
                continue
            yield {
                "source_path": path,
                "source_size": int(_resource_value(resource, "size", 0) or 0),
                "source_md5": str(_resource_value(resource, "md5", "") or "").lower(),
            }


def _file_md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - Yandex Disk exposes MD5 as its integrity hash.
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _s3_verified(s3: Any, row: Mapping[str, Any]) -> bool:
    try:
        response = s3.head_object(
            Bucket=str(row["target_bucket"]),
            Key=str(row["target_key"]),
        )
    except Exception:
        return False
    metadata = _mapping(response.get("Metadata"))
    return (
        int(response.get("ContentLength") or -1) == int(row.get("source_size") or 0)
        and str(metadata.get("source-md5") or "").lower()
        == str(row.get("source_md5") or "").lower()
    )


def _progress_payload(
    *,
    stage: str,
    current: int,
    total: int,
    bytes_completed: int,
    bytes_total: int,
    current_path: str = "",
) -> Dict[str, Any]:
    percent = (
        int((max(0, current) * 100) / total)
        if total > 0
        else (100 if stage == "completed" else 0)
    )
    return {
        "stage": str(stage),
        "current": int(current),
        "total": int(total),
        "percent": max(0, min(percent, 100)),
        "bytes_completed": int(bytes_completed),
        "bytes_total": int(bytes_total),
        "current_path": str(current_path),
    }


def _publish_progress(db: Any, run_id: int, progress: Dict[str, Any]) -> None:
    db.update_run_progress(run_id, progress)
    db.insert_event(
        "task.progress",
        task_id=TASK_ID,
        run_id=run_id,
        panel_id=PANEL_ID,
        payload={"status": "running", "progress": progress},
    )


def run_transfer(
    *,
    db: Any,
    yadisk: Any,
    s3: Any,
    settings: TransferSettings,
    workspace: Path,
    run_id: int,
    should_stop: Callable[[], bool],
) -> Dict[str, Any]:
    """Discover and move videos, checkpointing after each safe boundary."""
    workspace.mkdir(parents=True, exist_ok=True)
    _publish_progress(
        db,
        run_id,
        _progress_payload(
            stage="discovering", current=0, total=0, bytes_completed=0, bytes_total=0
        ),
    )

    discovered = 0
    for category, source_root in settings.source_dirs.items():
        for item in _walk_videos(yadisk, source_root, should_stop=should_stop):
            source_md5 = str(item.get("source_md5") or "").strip().lower()
            if not source_md5:
                print(
                    f"shayan yadisk_s3: skip path={item['source_path']} reason=missing_source_md5",
                    flush=True,
                )
                continue
            db.upsert_shayan_s3_transfer(
                source_path=item["source_path"],
                category=category,
                source_md5=source_md5,
                source_size=int(item["source_size"]),
                target_bucket=settings.target.bucket,
                target_key=destination_key(
                    source_root=source_root,
                    source_path=item["source_path"],
                    category=category,
                    prefix=settings.target.prefix,
                ),
            )
            discovered += 1
        if should_stop():
            break

    candidates = db.list_shayan_s3_transfer_candidates()
    total = len(candidates)
    bytes_total = sum(int(row.get("source_size") or 0) for row in candidates)
    processed = moved = reused = failed = bytes_completed = 0
    stopped = bool(should_stop())
    _publish_progress(
        db,
        run_id,
        _progress_payload(
            stage="transferring",
            current=0,
            total=total,
            bytes_completed=0,
            bytes_total=bytes_total,
        ),
    )
    print(
        f"shayan yadisk_s3: start discovered={discovered} candidates={total} bytes_total={bytes_total}",
        flush=True,
    )

    for row in candidates:
        if stopped:
            break
        if should_stop():
            stopped = True
            break
        source_path = str(row["source_path"])
        source_size = int(row.get("source_size") or 0)
        print(
            f"shayan yadisk_s3: process progress={processed + 1}/{total} "
            f"source_path={source_path} target=s3://{row['target_bucket']}/{row['target_key']}",
            flush=True,
        )
        try:
            if _s3_verified(s3, row):
                reused += 1
                db.mark_shayan_s3_transfer_state(source_path, status="uploaded")
            else:
                db.mark_shayan_s3_transfer_state(source_path, status="transferring")
                suffix = (
                    PurePosixPath(_plain_remote_path(source_path)).suffix or ".video"
                )
                temp_name = (
                    hashlib.sha256(source_path.encode("utf-8")).hexdigest() + suffix
                )
                local_path = workspace / temp_name
                try:
                    yadisk.download(source_path, str(local_path))
                    local_md5 = _file_md5(local_path)
                    if local_md5 != str(row.get("source_md5") or "").lower():
                        raise RuntimeError(
                            f"download hash mismatch expected={row.get('source_md5')} actual={local_md5}"
                        )
                    s3.upload_file(
                        str(local_path),
                        str(row["target_bucket"]),
                        str(row["target_key"]),
                        ExtraArgs={
                            "Metadata": {"source-md5": local_md5},
                            "ContentType": "application/octet-stream",
                        },
                    )
                finally:
                    local_path.unlink(missing_ok=True)
                if not _s3_verified(s3, row):
                    raise RuntimeError("S3 object verification failed after upload")
                db.mark_shayan_s3_transfer_state(source_path, status="uploaded")

            if yadisk.get_meta_or_none(source_path, fields=["type"]) is not None:
                yadisk.remove(source_path)
            db.mark_shayan_s3_transfer_state(source_path, status="moved")
            moved += 1
            bytes_completed += source_size
            print(
                f"shayan yadisk_s3: moved source_path={source_path} "
                f"target=s3://{row['target_bucket']}/{row['target_key']}",
                flush=True,
            )
        except Exception as exc:
            failed += 1
            db.mark_shayan_s3_transfer_state(
                source_path,
                status="failed",
                error_text=f"{type(exc).__name__}: {exc}",
            )
            print(
                f"shayan yadisk_s3: failed source_path={source_path} "
                f"error={type(exc).__name__}: {exc}",
                flush=True,
            )
        processed += 1
        _publish_progress(
            db,
            run_id,
            _progress_payload(
                stage="transferring",
                current=processed,
                total=total,
                bytes_completed=bytes_completed,
                bytes_total=bytes_total,
                current_path=source_path,
            ),
        )

    final_stage = "stopped" if stopped else "completed"
    final_progress = _progress_payload(
        stage=final_stage,
        current=processed,
        total=total,
        bytes_completed=bytes_completed,
        bytes_total=bytes_total,
    )
    _publish_progress(db, run_id, final_progress)
    summary = {
        "kind": "shayan.yadisk_s3_transfer_summary",
        "discovered": discovered,
        "considered": total,
        "processed": processed,
        "moved": moved,
        "reused": reused,
        "failed": failed,
        "bytes_moved": bytes_completed,
        "stopped": stopped,
        "target_bucket": settings.target.bucket,
        "target_prefix": settings.target.prefix,
    }
    print(
        f"shayan yadisk_s3: final {json.dumps(summary, ensure_ascii=False, sort_keys=True)}",
        flush=True,
    )
    return summary


def _run_id() -> int:
    value = str(os.environ.get("MANZARA_TASK_RUN_ID") or "").strip()
    if not value or not value.isdigit() or int(value) <= 0:
        raise RuntimeError("MANZARA_TASK_RUN_ID is required")
    return int(value)


def main() -> int:
    run_id = _run_id()
    app_settings = load_settings()
    transfer_settings = load_transfer_settings(load_runtime_config())
    db = Database(app_settings.database_url, schema=app_settings.database_schema)
    yadisk = YaDisk(transfer_settings.yadisk_token)
    if yadisk.check_token() is False:
        raise RuntimeError("Yandex Disk token validation failed")
    s3 = Session().client(
        "s3",
        aws_access_key_id=transfer_settings.target.access_key_id,
        aws_secret_access_key=transfer_settings.target.secret_access_key,
        endpoint_url=transfer_settings.target.endpoint_url,
        region_name=transfer_settings.target.region_name,
    )

    stop_state = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_state["requested"] = True
        print(
            "shayan yadisk_s3: graceful stop requested; finishing current file",
            flush=True,
        )

    signal.signal(signal.SIGINT, request_stop)
    workspace_root = flow_artifacts_dir("shayan") / "yadisk-s3-transfer"
    workspace_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"run-{run_id}-", dir=workspace_root
    ) as temp_dir:
        summary = run_transfer(
            db=db,
            yadisk=yadisk,
            s3=s3,
            settings=transfer_settings,
            workspace=Path(temp_dir),
            run_id=run_id,
            should_stop=lambda: bool(stop_state["requested"]),
        )
    emit_run_artifact(summary)
    return 1 if int(summary.get("failed") or 0) > 0 else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
