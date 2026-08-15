"""Shared, fail-closed WebDAV support for Shayan video uploads."""

from __future__ import annotations

import hashlib
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Mapping
from urllib.parse import quote, urlparse

import requests


_DAV = "DAV:"
_OC = "http://owncloud.org/ns"


@dataclass(frozen=True)
class NextcloudSettings:
    webdav_url: str
    username: str
    password: str
    target_dirs: Dict[str, str]


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int
    etag: str
    md5: str | None
    is_directory: bool = False


class WebDavError(RuntimeError):
    """Raised when Nextcloud cannot safely satisfy a WebDAV operation."""


class GracefulStopRequested(RuntimeError):
    """Raised at a safe WebDAV retry boundary after a stop request."""


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


def load_nextcloud_settings(payload: Mapping[str, Any]) -> NextcloudSettings:
    """Parse the shared Shayan destination contract for direct and migrated uploads."""
    nextcloud = _mapping(payload.get("nextcloud"))
    shayan_routes = _mapping(nextcloud.get("shayan"))
    webdav_url = _required(nextcloud, "webdav_url", "nextcloud").rstrip("/")
    parsed = urlparse(webdav_url)
    if parsed.scheme != "https" or "/remote.php/dav/files/" not in parsed.path:
        raise RuntimeError(
            "nextcloud.webdav_url must be an HTTPS Nextcloud files WebDAV URL"
        )
    target_dirs: Dict[str, str] = {}
    for category in ("cartoons", "shows"):
        route = _mapping(shayan_routes.get(category))
        if not route:
            continue
        target_dirs[category] = _normalize_remote_path(
            _required(route, "target_dir", f"nextcloud.shayan.{category}")
        )
    if not target_dirs:
        raise RuntimeError(
            "Configure at least one Nextcloud Shayan destination: "
            "nextcloud.shayan.cartoons or nextcloud.shayan.shows"
        )
    return NextcloudSettings(
        webdav_url=webdav_url,
        username=_required(nextcloud, "username", "nextcloud"),
        password=_required(nextcloud, "password", "nextcloud"),
        target_dirs=target_dirs,
    )


def temporary_remote_path(target_path: str, md5: str) -> str:
    """Return a deterministic resumable staging path beside the final file."""
    target = PurePosixPath(_normalize_remote_path(target_path))
    name = f".manzara-{str(md5).lower()}.uploading"
    return str(target.with_name(name))


class NextcloudWebDavClient:
    """Minimal fail-closed Nextcloud WebDAV client for large file transfers."""

    CHUNK_SIZE = 64 * 1024 * 1024

    def __init__(
        self,
        settings: NextcloudSettings,
        *,
        session: requests.Session | None = None,
        session_factory: Callable[[], requests.Session] = requests.Session,
        sleep: Callable[[float], None] = time.sleep,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self._session_factory = session_factory
        self._owns_session = session is None
        self.session = session or self._new_session()
        self.session.auth = (settings.username, settings.password)
        self._sleep = sleep
        self._should_stop = should_stop or (lambda: False)

    def _new_session(self) -> requests.Session:
        session = self._session_factory()
        session.auth = (self.settings.username, self.settings.password)
        return session

    def reset_transport(self) -> None:
        """Discard an owned pooled connection without changing credentials."""
        if not self._owns_session:
            return
        self.session.close()
        self.session = self._new_session()

    def _url(self, path: str) -> str:
        encoded = "/".join(
            quote(part, safe="")
            for part in _normalize_remote_path(path).strip("/").split("/")
            if part
        )
        return (
            f"{self.settings.webdav_url}/{encoded}"
            if encoded
            else self.settings.webdav_url
        )

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
        if response.status_code == 401:
            raise WebDavError(
                "Nextcloud authentication failed status=401; verify "
                "nextcloud.username and nextcloud.password (an app password may be required)"
            )
        body = str(response.text or "").strip().replace("\n", " ")[:500]
        raise WebDavError(
            f"Nextcloud {operation} failed status={response.status_code} body={body or '<empty>'}"
        )

    @staticmethod
    def _retry_delay(response: requests.Response, attempt: int) -> float:
        retry_after = str(response.headers.get("Retry-After") or "").strip()
        try:
            return max(1.0, min(float(retry_after), 120.0))
        except ValueError:
            return float(min(60 * (2 ** max(0, attempt - 1)), 300))

    def _wait(self, seconds: float) -> None:
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            if self._should_stop():
                raise GracefulStopRequested("WebDAV retry interrupted by stop request")
            interval = min(1.0, remaining)
            self._sleep(interval)
            remaining -= interval

    def _with_transient_retry(
        self,
        operation: str,
        request: Callable[[], requests.Response],
        *,
        retry_server_errors: bool = False,
    ) -> requests.Response:
        attempt = 0
        while True:
            response = request()
            rate_limited = response.status_code == 429
            server_error = retry_server_errors and response.status_code in {
                500,
                502,
                503,
                504,
            }
            if not rate_limited and not server_error:
                return response
            attempt += 1
            if rate_limited:
                delay = self._retry_delay(response, attempt)
                print(
                    f"shayan yadisk_webdav: webdav rate_limited operation={operation} "
                    f"attempt={attempt} retry_in_seconds={delay:g}",
                    flush=True,
                )
            else:
                delay = float(min(5 * (2 ** max(0, attempt - 1)), 60))
                body = str(response.text or "").strip().replace("\n", " ")[:300]
                print(
                    f"shayan yadisk_webdav: webdav server_error "
                    f"operation={operation} status={response.status_code} "
                    f"attempt={attempt} retry_in_seconds={delay:g} "
                    f"body={body or '<empty>'}",
                    flush=True,
                )
                self.reset_transport()
            self._wait(delay)

    def stat(self, path: str) -> RemoteFile | None:
        body = """<?xml version="1.0" encoding="UTF-8"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:getcontentlength/><d:getetag/><d:resourcetype/>
  </d:prop>
</d:propfind>"""
        response = self._with_transient_retry(
            "PROPFIND",
            lambda: self.session.request(
                "PROPFIND",
                self._url(path),
                headers={"Depth": "0", "Content-Type": "application/xml"},
                data=body.encode("utf-8"),
                timeout=(15, 120),
            ),
            retry_server_errors=True,
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
            raise WebDavError(
                "Nextcloud PROPFIND response has no successful properties"
            )
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
            response = self._with_transient_retry(
                "MKCOL",
                lambda: self.session.request(
                    "MKCOL",
                    self._url(current),
                    timeout=(15, 120),
                ),
            )
            self._expect(response, {201}, "MKCOL")

    def stream_md5(self, path: str) -> str:
        response = self._with_transient_retry(
            "GET",
            lambda: self.session.get(
                self._url(path),
                stream=True,
                timeout=(15, 3600),
            ),
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
        response = self._with_transient_retry(
            "chunk upload MKCOL",
            lambda: self.session.request(
                "MKCOL",
                self._upload_url(upload_id),
                headers={"Destination": destination},
                timeout=(15, 120),
            ),
        )
        self._expect(response, {201}, "chunk upload MKCOL")

        with local_path.open("rb") as handle:
            chunk_number = 0
            while chunk := handle.read(self.CHUNK_SIZE):
                chunk_number += 1
                response = self._with_transient_retry(
                    "chunk PUT",
                    lambda: self.session.put(
                        self._upload_url(upload_id, f"{chunk_number:05d}"),
                        data=chunk,
                        headers={
                            "Content-Length": str(len(chunk)),
                            "OC-Total-Length": str(size),
                            "Destination": destination,
                        },
                        timeout=(15, 3600),
                    ),
                )
                self._expect(response, {201, 204}, "chunk PUT")
                if on_progress is not None:
                    on_progress(min(chunk_number * self.CHUNK_SIZE, size), size)

        response = self._with_transient_retry(
            "chunk assembly MOVE",
            lambda: self.session.request(
                "MOVE",
                self._upload_url(upload_id, ".file"),
                headers={
                    "Destination": destination,
                    "OC-Total-Length": str(size),
                    "Overwrite": "T",
                },
                timeout=(15, 3600),
            ),
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
        response = self._with_transient_retry(
            "MOVE",
            lambda: self.session.request(
                "MOVE",
                self._url(source),
                headers={
                    "Destination": self._url(target),
                    "Overwrite": "T" if overwrite else "F",
                },
                timeout=(15, 3600),
            ),
        )
        self._expect(response, {201, 204}, "MOVE")

    def delete(self, path: str) -> None:
        response = self._with_transient_retry(
            "DELETE",
            lambda: self.session.delete(self._url(path), timeout=(15, 120)),
        )
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
