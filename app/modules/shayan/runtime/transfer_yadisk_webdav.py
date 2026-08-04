"""Copy Shayan videos from Yandex Disk into Nextcloud over WebDAV."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import signal
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, Mapping
from urllib.parse import quote, urlparse

import requests
from yadisk_client import YaDisk

from app.artifacts import flow_artifacts_dir
from app.db import Database
from app.run_artifact_channel import emit_run_artifact
from app.runtime_config import load_runtime_config
from app.settings import load_settings


TASK_ID = "shayan.transfer_yadisk_webdav"
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
_DAV = "DAV:"
_OC = "http://owncloud.org/ns"


@dataclass(frozen=True)
class NextcloudSettings:
    webdav_url: str
    username: str
    password: str
    target_dir: str


@dataclass(frozen=True)
class TransferSettings:
    yadisk_token: str
    source_dirs: Dict[str, str]
    nextcloud: NextcloudSettings


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int
    etag: str
    md5: str | None
    is_directory: bool = False


class WebDavError(RuntimeError):
    """Raised when Nextcloud cannot safely satisfy a WebDAV operation."""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _required(mapping: Mapping[str, Any], key: str, path: str) -> str:
    value = str(mapping.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required config value: {path}.{key}")
    return value


def _normalize_remote_path(path: str) -> str:
    parts = [part for part in str(path or "").strip().strip("/").split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError(f"Invalid WebDAV path: {path!r}")
    return "/" + "/".join(parts) if parts else "/"


def load_transfer_settings(payload: Mapping[str, Any]) -> TransferSettings:
    """Parse the single supported Yandex-to-Nextcloud configuration contract."""
    yandex = _mapping(payload.get("yandex"))
    disk = _mapping(yandex.get("disk"))
    shayan_sources = _mapping(disk.get("shayan"))
    nextcloud = _mapping(payload.get("nextcloud"))
    shayan_target = _mapping(nextcloud.get("shayan"))
    webdav_url = _required(nextcloud, "webdav_url", "nextcloud").rstrip("/")
    parsed = urlparse(webdav_url)
    if parsed.scheme != "https" or "/remote.php/dav/files/" not in parsed.path:
        raise RuntimeError(
            "nextcloud.webdav_url must be an HTTPS Nextcloud files WebDAV URL"
        )
    return TransferSettings(
        yadisk_token=_required(disk, "oauth_token", "yandex.disk"),
        source_dirs={
            "cartoons": _required(shayan_sources, "cartoons", "yandex.disk.shayan"),
            "shows": _required(shayan_sources, "shows", "yandex.disk.shayan"),
        },
        nextcloud=NextcloudSettings(
            webdav_url=webdav_url,
            username=_required(nextcloud, "username", "nextcloud"),
            password=_required(nextcloud, "password", "nextcloud"),
            target_dir=_normalize_remote_path(
                _required(shayan_target, "target_dir", "nextcloud.shayan")
            ),
        ),
    )


def _plain_yandex_path(path: str) -> str:
    value = str(path or "").strip()
    if value.startswith("disk:"):
        value = value[5:]
    return "/" + value.lstrip("/")


def remote_path(
    *,
    source_root: str,
    source_path: str,
    category: str,
    target_dir: str,
) -> str:
    """Map one Yandex path into a stable hierarchy-preserving DAV path."""
    root = PurePosixPath(_plain_yandex_path(source_root))
    source = PurePosixPath(_plain_yandex_path(source_path))
    try:
        relative = source.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(
            f"Source path {source_path!r} is outside root {source_root!r}"
        ) from exc
    return _normalize_remote_path(
        posixpath.join(target_dir, str(category).strip("/"), relative)
    )


def temporary_remote_path(target_path: str, md5: str) -> str:
    """Return a deterministic resumable staging path beside the final file."""
    target = PurePosixPath(_normalize_remote_path(target_path))
    name = f".{target.name}.manzara-{str(md5).lower()}.part"
    return str(target.with_name(name))


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
    return mime_type.startswith("video/") or (
        PurePosixPath(name).suffix.lower() in VIDEO_EXTENSIONS
    )


def _walk_videos(
    yadisk: Any,
    root: str,
    *,
    should_stop: Callable[[], bool],
) -> Iterable[Dict[str, Any]]:
    stack = [str(root).rstrip("/")]
    while stack and not should_stop():
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
    digest = hashlib.md5()  # noqa: S324 - Yandex exposes MD5 as content identity.
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class NextcloudWebDavClient:
    """Minimal fail-closed Nextcloud WebDAV client for large file transfers."""

    CHUNK_SIZE = 64 * 1024 * 1024

    def __init__(
        self,
        settings: NextcloudSettings,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.session.auth = (settings.username, settings.password)

    def _url(self, path: str) -> str:
        encoded = "/".join(
            quote(part, safe="")
            for part in _normalize_remote_path(path).strip("/").split("/")
            if part
        )
        return f"{self.settings.webdav_url}/{encoded}" if encoded else self.settings.webdav_url

    def _upload_url(self, upload_id: str, leaf: str = "") -> str:
        marker = "/remote.php/dav/files/"
        prefix, separator, _files_path = self.settings.webdav_url.partition(marker)
        if not separator:
            raise WebDavError("Nextcloud files WebDAV URL cannot form upload endpoint")
        url = (
            f"{prefix}/remote.php/dav/uploads/"
            f"{quote(self.settings.username, safe='')}/{quote(upload_id, safe='')}"
        )
        return f"{url}/{quote(leaf, safe='')}" if leaf else url

    @staticmethod
    def _expect(response: requests.Response, allowed: set[int], operation: str) -> None:
        if response.status_code in allowed:
            return
        body = str(response.text or "").strip().replace("\n", " ")[:500]
        raise WebDavError(
            f"Nextcloud {operation} failed status={response.status_code} body={body or '<empty>'}"
        )

    def stat(self, path: str) -> RemoteFile | None:
        body = """<?xml version="1.0" encoding="UTF-8"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:prop>
    <d:getcontentlength/><d:getetag/><d:resourcetype/><oc:checksums/>
  </d:prop>
</d:propfind>"""
        response = self.session.request(
            "PROPFIND",
            self._url(path),
            headers={"Depth": "0", "Content-Type": "application/xml"},
            data=body.encode("utf-8"),
            timeout=(15, 120),
        )
        if response.status_code == 404:
            return None
        self._expect(response, {207}, "PROPFIND")
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise WebDavError("Nextcloud PROPFIND returned invalid XML") from exc
        prop = None
        for propstat in root.findall(f".//{{{_DAV}}}propstat"):
            status = str(propstat.findtext(f"{{{_DAV}}}status") or "")
            if " 200 " in status:
                prop = propstat.find(f"{{{_DAV}}}prop")
                break
        if prop is None:
            raise WebDavError("Nextcloud PROPFIND response has no successful properties")
        resource_type = prop.find(f"{{{_DAV}}}resourcetype")
        is_directory = bool(
            resource_type is not None
            and resource_type.find(f"{{{_DAV}}}collection") is not None
        )
        size_text = str(prop.findtext(f"{{{_DAV}}}getcontentlength") or "0")
        etag = str(prop.findtext(f"{{{_DAV}}}getetag") or "").strip().strip('"')
        md5 = None
        checksums = prop.find(f"{{{_OC}}}checksums")
        if checksums is not None:
            for checksum in checksums.iter():
                value = str(checksum.text or "").strip()
                algorithm, separator, digest = value.partition(":")
                if separator and algorithm.lower() == "md5":
                    md5 = digest.lower()
                    break
        return RemoteFile(
            path=_normalize_remote_path(path),
            size=int(size_text or 0),
            etag=etag,
            md5=md5,
            is_directory=is_directory,
        )

    def ensure_directory(self, path: str) -> None:
        current = ""
        for part in _normalize_remote_path(path).strip("/").split("/"):
            if not part:
                continue
            current += "/" + part
            existing = self.stat(current)
            if existing is not None:
                if not existing.is_directory:
                    raise WebDavError(f"Nextcloud path is not a directory: {current}")
                continue
            response = self.session.request(
                "MKCOL",
                self._url(current),
                timeout=(15, 120),
            )
            self._expect(response, {201}, "MKCOL")

    def stream_md5(self, path: str) -> str:
        response = self.session.get(
            self._url(path),
            stream=True,
            timeout=(15, 3600),
        )
        self._expect(response, {200}, "GET")
        digest = hashlib.md5()  # noqa: S324 - must match Yandex identity.
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                digest.update(chunk)
        return digest.hexdigest()

    def upload(
        self,
        local_path: Path,
        path: str,
        *,
        md5: str,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> RemoteFile:
        size = local_path.stat().st_size
        upload_id = f"manzara-{uuid.uuid4()}"
        destination = self._url(path)
        response = self.session.request(
            "MKCOL",
            self._upload_url(upload_id),
            headers={"Destination": destination},
            timeout=(15, 120),
        )
        self._expect(response, {201}, "chunk upload MKCOL")

        with local_path.open("rb") as handle:
            chunk_number = 0
            while chunk := handle.read(self.CHUNK_SIZE):
                chunk_number += 1
                response = self.session.put(
                    self._upload_url(upload_id, f"{chunk_number:05d}"),
                    data=chunk,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "OC-Total-Length": str(size),
                        "Destination": destination,
                    },
                    timeout=(15, 3600),
                )
                self._expect(response, {201, 204}, "chunk PUT")
                if on_progress is not None:
                    on_progress(min(chunk_number * self.CHUNK_SIZE, size), size)

        response = self.session.request(
            "MOVE",
            self._upload_url(upload_id, ".file"),
            headers={
                "Destination": destination,
                "OC-Total-Length": str(size),
                "Overwrite": "T",
            },
            timeout=(15, 3600),
        )
        self._expect(response, {201, 204}, "chunk assembly MOVE")
        remote = self.stat(path)
        if remote is None or remote.is_directory or remote.size != size:
            raise WebDavError("Nextcloud assembled upload size verification failed")
        actual_md5 = self.stream_md5(path)
        if actual_md5 != md5:
            raise WebDavError(
                f"Nextcloud assembled upload hash mismatch expected={md5} "
                f"actual={actual_md5}"
            )
        return replace(remote, md5=md5)

    def move(self, source: str, target: str, *, overwrite: bool) -> None:
        response = self.session.request(
            "MOVE",
            self._url(source),
            headers={
                "Destination": self._url(target),
                "Overwrite": "T" if overwrite else "F",
            },
            timeout=(15, 3600),
        )
        self._expect(response, {201, 204}, "MOVE")

    def delete(self, path: str) -> None:
        response = self.session.delete(self._url(path), timeout=(15, 120))
        self._expect(response, {200, 202, 204, 404}, "DELETE")


def _verified_remote(
    webdav: Any,
    path: str,
    row: Mapping[str, Any],
    *,
    allow_checkpoint: bool,
) -> RemoteFile | None:
    remote = webdav.stat(path)
    if remote is None or remote.is_directory:
        return None
    expected_size = int(row.get("source_size") or 0)
    expected_md5 = str(row.get("source_md5") or "").lower()
    if remote.size != expected_size:
        return None
    if (
        allow_checkpoint
        and remote.etag
        and remote.etag == str(row.get("target_etag") or "")
        and expected_md5 == str(row.get("target_checksum") or "").lower()
    ):
        return replace(remote, md5=expected_md5)
    actual_md5 = str(webdav.stream_md5(path) or "").lower()
    return replace(remote, md5=expected_md5) if actual_md5 == expected_md5 else None


def _verified_after_move(
    webdav: Any,
    path: str,
    row: Mapping[str, Any],
) -> RemoteFile:
    """Confirm the validated staged file now exists at its final DAV path."""
    remote = webdav.stat(path)
    expected_size = int(row.get("source_size") or 0)
    expected_md5 = str(row.get("source_md5") or "").lower()
    if remote is None or remote.is_directory or remote.size != expected_size:
        raise RuntimeError("Nextcloud final file size verification failed")
    if remote.md5 and remote.md5 != expected_md5:
        raise RuntimeError(
            "Nextcloud final checksum changed during MOVE: "
            f"expected={expected_md5} actual={remote.md5}"
        )
    return replace(remote, md5=expected_md5)


def _progress_payload(
    *,
    stage: str,
    current: int,
    total: int,
    bytes_completed: int,
    bytes_total: int,
    current_path: str = "",
) -> Dict[str, Any]:
    if bytes_total > 0:
        percent = int((max(0, bytes_completed) * 100) / bytes_total)
    elif total > 0:
        percent = int((max(0, current) * 100) / total)
    else:
        percent = 100 if stage == "completed" else 0
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
    webdav: Any,
    settings: TransferSettings,
    workspace: Path,
    run_id: int,
    should_stop: Callable[[], bool],
) -> Dict[str, Any]:
    """Discover and copy videos, checkpointing after each safe boundary."""
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
                    f"shayan yadisk_webdav: skip path={item['source_path']} "
                    "reason=missing_source_md5",
                    flush=True,
                )
                continue
            db.upsert_shayan_webdav_transfer(
                source_path=item["source_path"],
                category=category,
                source_md5=source_md5,
                source_size=int(item["source_size"]),
                target_path=remote_path(
                    source_root=source_root,
                    source_path=item["source_path"],
                    category=category,
                    target_dir=settings.nextcloud.target_dir,
                ),
            )
            discovered += 1
        if should_stop():
            break

    candidates = db.list_shayan_webdav_transfer_candidates()
    total = len(candidates)
    bytes_total = sum(int(row.get("source_size") or 0) for row in candidates)
    processed = copied = reused = failed = 0
    bytes_processed = bytes_copied = 0
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
        f"shayan yadisk_webdav: start discovered={discovered} "
        f"candidates={total} bytes_total={bytes_total}",
        flush=True,
    )

    for row in candidates:
        if stopped or should_stop():
            stopped = True
            break
        source_path = str(row["source_path"])
        source_size = int(row.get("source_size") or 0)
        target_path = str(row["target_path"])
        source_md5 = str(row.get("source_md5") or "").lower()
        temporary_path = temporary_remote_path(target_path, source_md5)
        print(
            f"shayan yadisk_webdav: process progress={processed + 1}/{total} "
            f"source_path={source_path} target_path={target_path}",
            flush=True,
        )
        try:
            verified = _verified_remote(
                webdav,
                target_path,
                row,
                allow_checkpoint=True,
            )
            if verified is None:
                staged = _verified_remote(
                    webdav,
                    temporary_path,
                    row,
                    allow_checkpoint=False,
                )
                if staged is None:
                    if webdav.stat(temporary_path) is not None:
                        webdav.delete(temporary_path)
                    db.mark_shayan_webdav_transfer_state(
                        source_path,
                        status="transferring",
                    )
                    suffix = PurePosixPath(_plain_yandex_path(source_path)).suffix or ".video"
                    local_path = workspace / (
                        hashlib.sha256(source_path.encode("utf-8")).hexdigest() + suffix
                    )
                    try:
                        yadisk.download(source_path, str(local_path))
                        local_md5 = _file_md5(local_path)
                        if local_md5 != source_md5:
                            raise RuntimeError(
                                "download hash mismatch "
                                f"expected={source_md5} actual={local_md5}"
                            )
                        webdav.ensure_directory(str(PurePosixPath(target_path).parent))

                        def publish_upload_progress(
                            uploaded_bytes: int,
                            _file_size: int,
                        ) -> None:
                            _publish_progress(
                                db,
                                run_id,
                                _progress_payload(
                                    stage="transferring",
                                    current=processed,
                                    total=total,
                                    bytes_completed=bytes_processed + uploaded_bytes,
                                    bytes_total=bytes_total,
                                    current_path=source_path,
                                ),
                            )

                        staged = webdav.upload(
                            local_path,
                            temporary_path,
                            md5=local_md5,
                            on_progress=publish_upload_progress,
                        )
                    finally:
                        local_path.unlink(missing_ok=True)
                else:
                    reused += 1
                webdav.move(temporary_path, target_path, overwrite=True)
                verified = _verified_after_move(webdav, target_path, row)
            else:
                reused += 1

            db.mark_shayan_webdav_transfer_state(
                source_path,
                status="uploaded",
                target_etag=verified.etag,
                target_checksum=source_md5,
            )
            copied += 1
            bytes_copied += source_size
            print(
                f"shayan yadisk_webdav: copied source_path={source_path} "
                f"target_path={target_path}",
                flush=True,
            )
        except Exception as exc:
            failed += 1
            db.mark_shayan_webdav_transfer_state(
                source_path,
                status="failed",
                error_text=f"{type(exc).__name__}: {exc}",
            )
            print(
                f"shayan yadisk_webdav: failed source_path={source_path} "
                f"error={type(exc).__name__}: {exc}",
                flush=True,
            )
        processed += 1
        bytes_processed += source_size
        _publish_progress(
            db,
            run_id,
            _progress_payload(
                stage="transferring",
                current=processed,
                total=total,
                bytes_completed=bytes_processed,
                bytes_total=bytes_total,
                current_path=source_path,
            ),
        )

    final_stage = "stopped" if stopped else "completed"
    final_progress = _progress_payload(
        stage=final_stage,
        current=processed,
        total=total,
        bytes_completed=bytes_processed,
        bytes_total=bytes_total,
    )
    _publish_progress(db, run_id, final_progress)
    summary = {
        "kind": "shayan.yadisk_webdav_transfer_summary",
        "discovered": discovered,
        "considered": total,
        "processed": processed,
        "copied": copied,
        "reused": reused,
        "failed": failed,
        "bytes_copied": bytes_copied,
        "stopped": stopped,
        "target_dir": settings.nextcloud.target_dir,
    }
    print(
        f"shayan yadisk_webdav: final "
        f"{json.dumps(summary, ensure_ascii=False, sort_keys=True)}",
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
    webdav = NextcloudWebDavClient(transfer_settings.nextcloud)

    stop_state = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_state["requested"] = True
        print(
            "shayan yadisk_webdav: graceful stop requested; finishing current file",
            flush=True,
        )

    signal.signal(signal.SIGINT, request_stop)
    workspace_root = flow_artifacts_dir("shayan") / "yadisk-webdav-transfer"
    workspace_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"run-{run_id}-", dir=workspace_root
    ) as temp_dir:
        summary = run_transfer(
            db=db,
            yadisk=yadisk,
            webdav=webdav,
            settings=transfer_settings,
            workspace=Path(temp_dir),
            run_id=run_id,
            should_stop=lambda: bool(stop_state["requested"]),
        )
    emit_run_artifact(summary)
    return 1 if int(summary.get("failed") or 0) > 0 else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
