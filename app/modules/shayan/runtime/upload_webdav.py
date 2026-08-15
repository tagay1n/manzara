"""Upload locally downloaded Shayan videos directly to Hetzner WebDAV."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import signal
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Mapping, Sequence

from app.modules.shayan.runtime.webdav import (
    GracefulStopRequested,
    NextcloudSettings,
    NextcloudWebDavClient,
    _normalize_remote_path,
    _progress_payload,
    _verified_after_move,
    _verified_remote,
    load_nextcloud_settings,
    temporary_remote_path,
)
from app.db import Database
from app.run_artifact_channel import emit_run_artifact
from app.runtime_config import load_runtime_config
from app.settings import load_settings


TASK_ID = "shayan.upload_yadisk"  # Stable ID preserves task and run history.
PANEL_ID = "shayan"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload downloaded Shayan videos directly to Hetzner."
    )
    parser.add_argument("--output-path", required=True)
    return parser.parse_args(argv)


def _run_id() -> int:
    value = str(os.environ.get("MANZARA_TASK_RUN_ID") or "").strip()
    if not value.isdigit() or int(value) <= 0:
        raise RuntimeError("MANZARA_TASK_RUN_ID is required")
    return int(value)


def _file_md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - interoperable content identity.
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_file(payload: Mapping[str, Any], output_path: Path) -> Path | None:
    raw = str(payload.get("file") or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else (output_path / candidate).resolve()


def _relative_video_path(local_file: Path, output_path: Path) -> str:
    try:
        return local_file.relative_to(output_path).as_posix().lstrip("/")
    except ValueError:
        marker = "/videos/"
        raw = local_file.as_posix()
        index = raw.rfind(marker)
        return raw[index + 1 :].lstrip("/") if index >= 0 else local_file.name


def _category(
    payload: Mapping[str, Any],
    local_file: Path,
    output_path: Path,
) -> str:
    direct = str(payload.get("category") or "").strip().lower()
    if direct:
        return direct
    parts = [
        part
        for part in _relative_video_path(local_file, output_path).strip("/").split("/")
        if part
    ]
    if len(parts) >= 2 and parts[0] == "videos":
        return parts[1].lower()
    return parts[0].lower() if parts else ""


def _target_path(
    *,
    local_file: Path,
    output_path: Path,
    category: str,
    target_dir: str,
) -> str:
    relative = _relative_video_path(local_file, output_path).strip("/")
    for prefix in (f"videos/{category}/", f"{category}/"):
        if relative.startswith(prefix):
            relative = relative[len(prefix) :].strip("/")
            break
    if not relative:
        relative = local_file.name
    return _normalize_remote_path(posixpath.join(target_dir, relative))


def _publish_progress(db: Any, run_id: int, progress: Dict[str, Any]) -> None:
    db.update_run_progress(run_id, progress)
    db.insert_event(
        "task.progress",
        task_id=TASK_ID,
        run_id=run_id,
        panel_id=PANEL_ID,
        payload={"status": "running", "progress": progress},
    )


def _summary(
    *,
    settings: NextcloudSettings,
    considered: int,
    processed: int,
    uploaded: int,
    reused: int,
    failed: int,
    missing_local: int,
    deleted_local: int,
    bytes_uploaded: int,
    stopped: bool,
) -> Dict[str, Any]:
    return {
        "kind": "shayan.webdav_upload_summary",
        "target_dirs": dict(settings.target_dirs),
        "considered": considered,
        "processed": processed,
        "uploaded": uploaded,
        "reused": reused,
        "failed": failed,
        "missing_local": missing_local,
        "deleted_local": deleted_local,
        "bytes_uploaded": bytes_uploaded,
        "stopped": stopped,
    }


def run_upload(
    *,
    db: Any,
    webdav: Any,
    settings: NextcloudSettings,
    output_path: Path,
    run_id: int,
    should_stop: Callable[[], bool],
) -> Dict[str, Any]:
    """Upload each pending manifest file and checkpoint every verified boundary."""
    _publish_progress(
        db,
        run_id,
        _progress_payload(
            stage="connecting", current=0, total=0, bytes_completed=0, bytes_total=0
        ),
    )
    try:
        for category, target_dir in settings.target_dirs.items():
            remote = webdav.stat(target_dir)
            print(
                "shayan webdav_upload: connection_check success "
                f"category={category} target_dir={target_dir} "
                f"target_exists={remote is not None}",
                flush=True,
            )
    except GracefulStopRequested:
        _publish_progress(
            db,
            run_id,
            _progress_payload(
                stage="stopped",
                current=0,
                total=0,
                bytes_completed=0,
                bytes_total=0,
            ),
        )
        result = _summary(
            settings=settings,
            considered=0,
            processed=0,
            uploaded=0,
            reused=0,
            failed=0,
            missing_local=0,
            deleted_local=0,
            bytes_uploaded=0,
            stopped=True,
        )
        print(
            f"shayan webdav_upload: final {json.dumps(result, ensure_ascii=False, sort_keys=True)}",
            flush=True,
        )
        return result

    candidates = db.list_shayan_manifest_webdav_upload_candidates(limit=5000)
    total = len(candidates)
    bytes_total = 0
    for row in candidates:
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        path = _local_file(payload, output_path)
        if path is not None and path.is_file():
            bytes_total += path.stat().st_size
        else:
            bytes_total += int(row.get("source_size") or 0)

    processed = uploaded = reused = failed = missing_local = deleted_local = 0
    bytes_processed = bytes_uploaded = 0
    stopped = False
    _publish_progress(
        db,
        run_id,
        _progress_payload(
            stage="uploading",
            current=0,
            total=total,
            bytes_completed=0,
            bytes_total=bytes_total,
        ),
    )
    print(
        f"shayan webdav_upload: start considered={total} bytes_total={bytes_total}",
        flush=True,
    )

    for row in candidates:
        if should_stop():
            stopped = True
            break
        entry_key = str(row.get("entry_key") or "").strip()
        payload_hash = str(row.get("payload_hash") or "").strip()
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        local_path = _local_file(payload, output_path)
        item_size = int(row.get("source_size") or 0)
        try:
            if not entry_key or not payload_hash or local_path is None:
                raise RuntimeError("local_file_missing_in_payload")
            category = _category(payload, local_path, output_path)
            target_dir = str(settings.target_dirs.get(category) or "").strip()
            if not target_dir:
                raise RuntimeError(
                    f"missing_target_dir_for_category:{category or 'unknown'}"
                )
            target_path = _target_path(
                local_file=local_path,
                output_path=output_path,
                category=category,
                target_dir=target_dir,
            )

            if local_path.is_file():
                source_size = local_path.stat().st_size
                source_md5 = _file_md5(local_path)
                item_size = source_size
                db.mark_shayan_manifest_webdav_upload_started(
                    entry_key,
                    remote_path=target_path,
                    source_md5=source_md5,
                    source_size=source_size,
                    payload_hash=payload_hash,
                )
            else:
                source_size = int(row.get("source_size") or 0)
                source_md5 = str(row.get("source_md5") or "").strip().lower()
                checkpoint_target = str(row.get("target_path") or "").strip()
                if (
                    not source_md5
                    or source_size < 0
                    or checkpoint_target != target_path
                ):
                    raise FileNotFoundError(f"local_file_missing:{local_path}")

            verify_row = {
                "source_size": source_size,
                "source_md5": source_md5,
                "target_etag": row.get("target_etag"),
                "target_checksum": row.get("target_checksum"),
            }
            verified = _verified_remote(
                webdav,
                target_path,
                verify_row,
                allow_checkpoint=True,
            )
            if verified is None:
                if not local_path.is_file():
                    raise FileNotFoundError(f"local_file_missing:{local_path}")
                staged_path = temporary_remote_path(target_path, source_md5)
                staged = _verified_remote(
                    webdav,
                    staged_path,
                    verify_row,
                    allow_checkpoint=False,
                )
                if staged is None:
                    if webdav.stat(staged_path) is not None:
                        webdav.delete(staged_path)
                    webdav.ensure_directory(str(PurePosixPath(target_path).parent))

                    def publish_file_progress(completed: int, _total: int) -> None:
                        _publish_progress(
                            db,
                            run_id,
                            _progress_payload(
                                stage="uploading",
                                current=processed,
                                total=total,
                                bytes_completed=bytes_processed + completed,
                                bytes_total=bytes_total,
                                current_path=str(local_path),
                            ),
                        )

                    webdav.upload(
                        local_path,
                        staged_path,
                        md5=source_md5,
                        on_progress=publish_file_progress,
                    )
                else:
                    reused += 1
                webdav.move(staged_path, target_path, overwrite=True)
                verified = _verified_after_move(webdav, target_path, verify_row)
                bytes_uploaded += source_size
            else:
                reused += 1

            if local_path.exists():
                local_path.unlink()
                deleted_local += 1
            db.mark_shayan_manifest_webdav_uploaded(
                entry_key,
                remote_path=target_path,
                payload_hash=payload_hash,
                target_etag=verified.etag,
                target_checksum=source_md5,
            )
            uploaded += 1
            print(
                "shayan webdav_upload: uploaded "
                f"entry_key={entry_key} category={category} target_path={target_path} "
                "local_deleted=true",
                flush=True,
            )
        except GracefulStopRequested:
            stopped = True
            break
        except Exception as exc:
            failed += 1
            if isinstance(exc, FileNotFoundError) or "local_file_missing" in str(exc):
                missing_local += 1
            if entry_key:
                db.mark_shayan_manifest_webdav_failed(
                    entry_key,
                    error_text=f"{type(exc).__name__}: {exc}",
                )
            print(
                f"shayan webdav_upload: failed entry_key={entry_key or '<missing>'} "
                f"error={type(exc).__name__}: {exc}",
                flush=True,
            )
        processed += 1
        bytes_processed += item_size
        _publish_progress(
            db,
            run_id,
            _progress_payload(
                stage="uploading",
                current=processed,
                total=total,
                bytes_completed=bytes_processed,
                bytes_total=bytes_total,
            ),
        )

    _publish_progress(
        db,
        run_id,
        _progress_payload(
            stage="stopped" if stopped else "completed",
            current=processed,
            total=total,
            bytes_completed=bytes_processed,
            bytes_total=bytes_total,
        ),
    )
    result = _summary(
        settings=settings,
        considered=total,
        processed=processed,
        uploaded=uploaded,
        reused=reused,
        failed=failed,
        missing_local=missing_local,
        deleted_local=deleted_local,
        bytes_uploaded=bytes_uploaded,
        stopped=stopped,
    )
    print(
        f"shayan webdav_upload: final {json.dumps(result, ensure_ascii=False, sort_keys=True)}",
        flush=True,
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    run_id = _run_id()
    app_settings = load_settings()
    nextcloud_settings = load_nextcloud_settings(load_runtime_config())
    db = Database(app_settings.database_url, schema=app_settings.database_schema)
    stop_state = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_state["requested"] = True
        print(
            "shayan webdav_upload: graceful stop requested; finishing current boundary",
            flush=True,
        )

    signal.signal(signal.SIGINT, request_stop)
    webdav = NextcloudWebDavClient(
        nextcloud_settings,
        should_stop=lambda: bool(stop_state["requested"]),
    )
    result = run_upload(
        db=db,
        webdav=webdav,
        settings=nextcloud_settings,
        output_path=Path(args.output_path).expanduser(),
        run_id=run_id,
        should_stop=lambda: bool(stop_state["requested"]),
    )
    emit_run_artifact(result)
    return 1 if int(result.get("failed") or 0) > 0 else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
