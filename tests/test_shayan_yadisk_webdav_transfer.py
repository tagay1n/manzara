"""Tests for resumable Shayan Yandex Disk to Nextcloud transfers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

import pytest

from app.modules.shayan.runtime.transfer_yadisk_webdav import (
    NextcloudSettings,
    NextcloudWebDavClient,
    RemoteFile,
    TransferSettings,
    WebDavError,
    load_nextcloud_settings,
    load_transfer_settings,
    remote_path,
    run_transfer,
    temporary_remote_path,
)


class _HttpResponse:
    def __init__(
        self,
        status_code: int,
        *,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.headers = dict(headers or {})

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]


class _HttpSession:
    def __init__(self) -> None:
        self.auth = None
        self.closed = False
        self.request_responses: list[_HttpResponse] = []
        self.put_responses: list[_HttpResponse] = []
        self.get_responses: list[_HttpResponse] = []
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs):  # noqa: ANN003
        self.calls.append((method, url, kwargs))
        return self.request_responses.pop(0)

    def put(self, url: str, **kwargs):  # noqa: ANN003
        self.calls.append(("PUT", url, kwargs))
        return self.put_responses.pop(0)

    def get(self, url: str, **kwargs):  # noqa: ANN003
        self.calls.append(("GET", url, kwargs))
        return self.get_responses.pop(0)

    def delete(self, url: str, **kwargs):  # noqa: ANN003
        self.calls.append(("DELETE", url, kwargs))
        return self.request_responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _propfind_response(*, size: int, md5: str | None, etag: str = "etag-1") -> bytes:
    checksum = f"<oc:checksum>MD5:{md5}</oc:checksum>" if md5 else ""
    return f"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response><d:propstat><d:prop>
    <d:getcontentlength>{size}</d:getcontentlength>
    <d:getetag>\"{etag}\"</d:getetag>
    <d:resourcetype/>
    <oc:checksums>{checksum}</oc:checksums>
  </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
</d:multistatus>""".encode()


class _FakeYaDisk:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.removed: list[str] = []
        self.remove_kwargs: list[dict] = []
        self.metadata_override: dict[str, bytes] = {}
        self.before_remove: Callable[[str], None] | None = None

    def listdir(self, path, **_kwargs):  # noqa: ANN001
        prefix = str(path).rstrip("/") + "/"
        direct: dict[str, dict] = {}
        for source_path, content in self.files.items():
            if not source_path.startswith(prefix):
                continue
            remainder = source_path[len(prefix) :]
            first, _, tail = remainder.partition("/")
            child_path = prefix + first
            if tail:
                direct[first] = {"name": first, "path": child_path, "type": "dir"}
            else:
                direct[first] = {
                    "name": first,
                    "path": source_path,
                    "type": "file",
                    "size": len(content),
                    "md5": hashlib.md5(content).hexdigest(),  # noqa: S324
                    "mime_type": "video/x-matroska",
                }
        return iter(direct.values())

    def download(self, source_path, target_path):  # noqa: ANN001
        Path(target_path).write_bytes(self.files[str(source_path)])

    def get_meta_or_none(self, source_path, **_kwargs):  # noqa: ANN001
        content = self.files.get(str(source_path))
        if content is None:
            return None
        content = self.metadata_override.get(str(source_path), content)
        return {
            "path": str(source_path),
            "type": "file",
            "size": len(content),
            "md5": hashlib.md5(content).hexdigest(),  # noqa: S324
        }

    def remove(self, source_path, **kwargs):  # noqa: ANN001
        if self.before_remove is not None:
            self.before_remove(str(source_path))
        self.removed.append(str(source_path))
        self.remove_kwargs.append(dict(kwargs))
        self.files.pop(str(source_path), None)


class _FakeWebDav:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.checksums: dict[str, str] = {}
        self.uploaded: list[str] = []
        self.moves: list[tuple[str, str]] = []
        self.stat_error: Exception | None = None
        self.transport_resets = 0

    def reset_transport(self) -> None:
        self.transport_resets += 1

    def ensure_directory(self, _path: str) -> None:
        return None

    def stat(self, path: str) -> RemoteFile | None:
        if self.stat_error is not None:
            raise self.stat_error
        content = self.files.get(path)
        if content is None:
            return None
        return RemoteFile(
            path=path,
            size=len(content),
            etag=f'etag-{hashlib.sha1(content).hexdigest()}',  # noqa: S324
            md5=self.checksums.get(path),
        )

    def upload(
        self,
        local_path: Path,
        path: str,
        *,
        md5: str,
        on_progress=None,  # noqa: ANN001
    ) -> RemoteFile:
        content = local_path.read_bytes()
        if on_progress is not None:
            on_progress(len(content), len(content))
        self.files[path] = content
        self.checksums[path] = md5
        self.uploaded.append(path)
        remote = self.stat(path)
        assert remote is not None
        return remote

    def stream_md5(self, path: str) -> str:
        return hashlib.md5(self.files[path]).hexdigest()  # noqa: S324

    def move(self, source: str, target: str, *, overwrite: bool) -> None:
        if not overwrite and target in self.files:
            raise RuntimeError("target exists")
        self.files[target] = self.files.pop(source)
        checksum = self.checksums.pop(source, None)
        if checksum:
            self.checksums[target] = checksum
        self.moves.append((source, target))

    def delete(self, path: str) -> None:
        self.files.pop(path, None)
        self.checksums.pop(path, None)


class _FakeDb:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.progress: list[dict] = []
        self.events: list[dict] = []

    def upsert_shayan_webdav_transfer(self, **item):  # noqa: ANN003
        source_path = str(item["source_path"])
        previous = self.rows.get(source_path, {})
        identity_fields = (
            "source_md5",
            "source_size",
            "target_path",
        )
        same_source = bool(previous) and all(
            previous.get(field) == item.get(field) for field in identity_fields
        )
        status = str(previous.get("status") or "pending") if same_source else "pending"
        self.rows[source_path] = {**previous, **item, "status": status}
        if not same_source:
            self.rows[source_path]["target_etag"] = None
            self.rows[source_path]["target_checksum"] = None

    def list_shayan_webdav_transfer_candidates(self):
        return [
            dict(row)
            for row in self.rows.values()
            if row.get("status") not in {"uploaded", "moved"}
        ]

    def mark_shayan_webdav_transfer_state(
        self,
        source_path,
        *,
        status,
        error_text=None,
        target_etag=None,
        target_checksum=None,
    ):  # noqa: ANN001
        row = self.rows[str(source_path)]
        row["status"] = str(status)
        row["error_text"] = error_text
        if target_etag is not None:
            row["target_etag"] = target_etag
        if target_checksum is not None:
            row["target_checksum"] = target_checksum

    def update_run_progress(self, run_id, payload):  # noqa: ANN001
        self.progress.append({"run_id": int(run_id), **dict(payload)})

    def insert_event(self, event_type, **kwargs):  # noqa: ANN001, ANN003
        self.events.append({"type": event_type, **kwargs})


def _settings() -> TransferSettings:
    return TransferSettings(
        yadisk_token="token",
        source_dirs={"cartoons": "/source/cartoons", "shows": "/source/shows"},
        nextcloud=NextcloudSettings(
            webdav_url="https://cloud.example.test/remote.php/dav/files/Admin",
            username="Admin",
            password="password",
            target_dirs={
                "cartoons": "/Manzara/Shayan/cartoons",
                "shows": "/Manzara/Shayan/shows",
            },
        ),
    )


def test_load_transfer_settings_requires_nextcloud_contract() -> None:
    payload = {
        "yandex": {
            "disk": {
                "oauth_token": "token",
                "shayan": {"cartoons": "/cartoons", "shows": "/shows"},
            }
        },
        "nextcloud": {
            "webdav_url": "https://cloud.example.test/remote.php/dav/files/Admin",
            "username": "Admin",
            "password": "password",
            "shayan": {
                "cartoons": {
                    "source_dir": "/source/root",
                    "target_dir": "/Безнең тәҗрибә/Мультфильмнар",
                }
            },
        },
    }

    settings = load_transfer_settings(payload)
    assert settings.source_dirs["cartoons"] == "/source/root"
    assert "shows" not in settings.source_dirs
    assert settings.nextcloud.username == "Admin"
    assert settings.nextcloud.target_dirs == {
        "cartoons": "/Безнең тәҗрибә/Мультфильмнар"
    }

    del payload["nextcloud"]["password"]
    with pytest.raises(RuntimeError, match="nextcloud.password"):
        load_transfer_settings(payload)

    payload["nextcloud"]["password"] = "password"
    payload["nextcloud"]["shayan"]["cartoons"]["target_dir"] = (
        "/Manzara/../Elsewhere"
    )
    with pytest.raises(ValueError, match="Invalid WebDAV path"):
        load_transfer_settings(payload)


def test_direct_upload_settings_need_no_yandex_source() -> None:
    settings = load_nextcloud_settings(
        {
            "nextcloud": {
                "webdav_url": "https://cloud.example/remote.php/dav/files/Admin",
                "username": "Admin",
                "password": "password",
                "shayan": {"shows": {"target_dir": "/Hetzner/Shows"}},
            }
        }
    )

    assert settings.target_dirs == {"shows": "/Hetzner/Shows"}


def test_webdav_stat_uses_standard_properties_and_encodes_path() -> None:
    session = _HttpSession()
    session.request_responses.append(
        _HttpResponse(
            207,
            content=_propfind_response(size=10, md5="a" * 32),
        )
    )
    client = NextcloudWebDavClient(_settings().nextcloud, session=session)

    remote = client.stat("/Manzara/Shayan/Тест видео.mkv")

    assert remote == RemoteFile(
        path="/Manzara/Shayan/Тест видео.mkv",
        size=10,
        etag="etag-1",
        md5="a" * 32,
    )
    assert session.auth == ("Admin", "password")
    assert "%D0%A2%D0%B5%D1%81%D1%82%20%D0%B2%D0%B8%D0%B4%D0%B5%D0%BE.mkv" in (
        session.calls[0][1]
    )
    request_body = session.calls[0][2]["data"]
    assert b"getcontentlength" in request_body
    assert b"getetag" in request_body
    assert b"resourcetype" in request_body
    assert b"checksums" not in request_body


def test_webdav_upload_uses_nextcloud_chunking_and_verifies_assembled_file(
    tmp_path: Path,
) -> None:
    content = b"video-data"
    expected_md5 = hashlib.md5(content).hexdigest()  # noqa: S324
    local_path = tmp_path / "video.mkv"
    local_path.write_bytes(content)
    session = _HttpSession()
    session.request_responses.extend(
        [
            _HttpResponse(201),
            _HttpResponse(201),
            _HttpResponse(
                207,
                content=_propfind_response(size=len(content), md5=None),
            ),
        ]
    )
    session.put_responses.append(_HttpResponse(201))
    session.get_responses.append(_HttpResponse(200, content=content))
    client = NextcloudWebDavClient(_settings().nextcloud, session=session)

    remote = client.upload(local_path, "/target/video.mkv", md5=expected_md5)

    assert remote.md5 == expected_md5
    mkcol_call = next(call for call in session.calls if call[0] == "MKCOL")
    assert "/remote.php/dav/uploads/Admin/manzara-" in mkcol_call[1]
    put_call = next(call for call in session.calls if call[0] == "PUT")
    assert put_call[1].endswith("/00001")
    assert put_call[2]["headers"]["OC-Total-Length"] == str(len(content))
    assert put_call[2]["headers"]["Destination"].endswith("/target/video.mkv")
    move_call = next(call for call in session.calls if call[0] == "MOVE")
    assert move_call[1].endswith("/.file")
    assert move_call[2]["headers"]["Destination"].endswith("/target/video.mkv")
    assert any(call[0] == "GET" for call in session.calls)


def test_webdav_chunk_upload_splits_large_files_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_path = tmp_path / "video.mkv"
    local_path.write_bytes(b"abcdefghij")
    session = _HttpSession()
    session.request_responses.extend(
        [
            _HttpResponse(201),
            _HttpResponse(201),
            _HttpResponse(207, content=_propfind_response(size=10, md5=None)),
        ]
    )
    session.put_responses.extend([_HttpResponse(201), _HttpResponse(201)])
    session.get_responses.append(_HttpResponse(200, content=b"abcdefghij"))
    client = NextcloudWebDavClient(_settings().nextcloud, session=session)
    monkeypatch.setattr(client, "CHUNK_SIZE", 5)
    progress: list[tuple[int, int]] = []

    client.upload(
        local_path,
        "/target/video.mkv",
        md5=hashlib.md5(b"abcdefghij").hexdigest(),  # noqa: S324
        on_progress=lambda completed, total: progress.append((completed, total)),
    )

    chunk_calls = [call for call in session.calls if call[0] == "PUT"]
    assert [call[1].rsplit("/", 1)[-1] for call in chunk_calls] == ["00001", "00002"]
    assert [call[2]["headers"]["Content-Length"] for call in chunk_calls] == ["5", "5"]
    assert progress == [(5, 10), (10, 10)]


def test_webdav_operational_error_is_not_treated_as_missing() -> None:
    session = _HttpSession()
    session.request_responses.append(_HttpResponse(400, content=b"invalid request"))
    client = NextcloudWebDavClient(_settings().nextcloud, session=session)

    with pytest.raises(WebDavError, match="status=400"):
        client.stat("/target/video.mkv")


def test_webdav_propfind_retries_transient_server_error() -> None:
    session = _HttpSession()
    session.request_responses.extend(
        [
            _HttpResponse(500, content=b"transient type error"),
            _HttpResponse(404),
        ]
    )
    sleeps: list[float] = []
    client = NextcloudWebDavClient(
        _settings().nextcloud,
        session=session,
        sleep=sleeps.append,
    )

    remote = client.stat("/target/missing-video.mkv")

    assert remote is None
    assert sleeps == [1.0] * 5
    assert [call[0] for call in session.calls] == ["PROPFIND", "PROPFIND"]


def test_webdav_propfind_reopens_owned_session_after_server_error() -> None:
    failed_session = _HttpSession()
    failed_session.request_responses.append(_HttpResponse(500))
    recovered_session = _HttpSession()
    recovered_session.request_responses.append(_HttpResponse(404))
    sessions = iter([failed_session, recovered_session])
    sleeps: list[float] = []
    client = NextcloudWebDavClient(
        _settings().nextcloud,
        session_factory=lambda: next(sessions),
        sleep=sleeps.append,
    )

    remote = client.stat("/target/missing-video.mkv")

    assert remote is None
    assert failed_session.closed is True
    assert recovered_session.auth == ("Admin", "password")
    assert sleeps == [1.0] * 5


def test_webdav_rate_limit_waits_and_retries_same_request() -> None:
    session = _HttpSession()
    session.request_responses.extend(
        [
            _HttpResponse(429, headers={"Retry-After": "7"}),
            _HttpResponse(207, content=_propfind_response(size=10, md5=None)),
        ]
    )
    sleeps: list[float] = []
    client = NextcloudWebDavClient(
        _settings().nextcloud,
        session=session,
        sleep=sleeps.append,
    )

    remote = client.stat("/target/video.mkv")

    assert remote is not None
    assert sleeps == [1.0] * 7
    assert [call[0] for call in session.calls] == ["PROPFIND", "PROPFIND"]


def test_webdav_authentication_failure_does_not_retry() -> None:
    session = _HttpSession()
    session.request_responses.append(_HttpResponse(401))
    sleeps: list[float] = []
    client = NextcloudWebDavClient(
        _settings().nextcloud,
        session=session,
        sleep=sleeps.append,
    )

    with pytest.raises(WebDavError, match="authentication failed"):
        client.stat("/target/video.mkv")

    assert sleeps == []
    assert len(session.calls) == 1


def test_remote_paths_preserve_category_hierarchy_and_temp_is_stable() -> None:
    target = remote_path(
        source_root="/source",
        source_path="/source/cartoons/Program/S01/S01E01.mkv",
        target_dir="/Безнең тәҗрибә/Мультфильмнар",
    )
    assert target == (
        "/Безнең тәҗрибә/Мультфильмнар/cartoons/Program/S01/S01E01.mkv"
    )
    assert temporary_remote_path(target, "a" * 32) == (
        "/Безнең тәҗрибә/Мультфильмнар/cartoons/Program/S01/"
        ".manzara-"
        + "a" * 32
        + ".uploading"
    )


def test_transfer_verifies_webdav_and_keeps_yadisk_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = "/source/cartoons/Program/S01/S01E01.mkv"
    content = b"video-data"
    target = "/Manzara/Shayan/cartoons/Program/S01/S01E01.mkv"
    yadisk = _FakeYaDisk({source: content})
    webdav = _FakeWebDav()
    db = _FakeDb()

    result = run_transfer(
        db=db,
        yadisk=yadisk,
        webdav=webdav,
        settings=_settings(),
        workspace=tmp_path,
        run_id=41,
        should_stop=lambda: False,
    )

    assert result["copied"] == 1
    assert result["failed"] == 0
    assert len(webdav.uploaded) == 1
    assert webdav.moves[-1][1] == target
    assert yadisk.removed == []
    assert source in yadisk.files
    assert db.rows[source]["status"] == "uploaded"
    assert db.progress[-1]["percent"] == 100
    output = capsys.readouterr().out
    assert "connection_check success category=cartoons" in output
    assert "discovery start category=cartoons" in output


def test_transfer_reuses_verified_final_without_uploading(tmp_path: Path) -> None:
    source = "/source/shows/Program/S01/S01E01.mkv"
    content = b"video-data"
    source_md5 = hashlib.md5(content).hexdigest()  # noqa: S324
    target = "/Manzara/Shayan/shows/Program/S01/S01E01.mkv"
    yadisk = _FakeYaDisk({source: content})
    webdav = _FakeWebDav()
    webdav.files[target] = content
    webdav.checksums[target] = source_md5
    db = _FakeDb()

    result = run_transfer(
        db=db,
        yadisk=yadisk,
        webdav=webdav,
        settings=_settings(),
        workspace=tmp_path,
        run_id=42,
        should_stop=lambda: False,
    )

    assert result["copied"] == 1
    assert result["reused"] == 1
    assert webdav.uploaded == []
    assert yadisk.removed == []
    assert source in yadisk.files


def test_transfer_does_not_trust_unverified_nextcloud_checksum(tmp_path: Path) -> None:
    source = "/source/shows/Program/S01/S01E01.mkv"
    content = b"video-data"
    source_md5 = hashlib.md5(content).hexdigest()  # noqa: S324
    target = "/Manzara/Shayan/shows/Program/S01/S01E01.mkv"
    yadisk = _FakeYaDisk({source: content})
    webdav = _FakeWebDav()
    webdav.files[target] = b"other-data"
    webdav.checksums[target] = source_md5
    db = _FakeDb()

    result = run_transfer(
        db=db,
        yadisk=yadisk,
        webdav=webdav,
        settings=_settings(),
        workspace=tmp_path,
        run_id=50,
        should_stop=lambda: False,
    )

    assert result["copied"] == 1
    assert len(webdav.uploaded) == 1
    assert webdav.files[target] == content
    assert yadisk.removed == []
    assert source in yadisk.files


def test_transfer_reuses_verified_temporary_upload_after_crash(tmp_path: Path) -> None:
    source = "/source/shows/Program/S01/S01E01.mkv"
    content = b"video-data"
    source_md5 = hashlib.md5(content).hexdigest()  # noqa: S324
    target = "/Manzara/Shayan/shows/Program/S01/S01E01.mkv"
    temporary = temporary_remote_path(target, source_md5)
    yadisk = _FakeYaDisk({source: content})
    webdav = _FakeWebDav()
    webdav.files[temporary] = content
    webdav.checksums[temporary] = source_md5
    db = _FakeDb()

    result = run_transfer(
        db=db,
        yadisk=yadisk,
        webdav=webdav,
        settings=_settings(),
        workspace=tmp_path,
        run_id=43,
        should_stop=lambda: False,
    )

    assert result["copied"] == 1
    assert result["reused"] == 1
    assert webdav.uploaded == []
    assert webdav.moves == [(temporary, target)]


def test_transfer_stops_before_next_file_boundary(tmp_path: Path) -> None:
    files = {
        "/source/cartoons/A/S01/one.mkv": b"one",
        "/source/cartoons/A/S01/two.mkv": b"two",
    }
    yadisk = _FakeYaDisk(files)
    webdav = _FakeWebDav()
    db = _FakeDb()

    result = run_transfer(
        db=db,
        yadisk=yadisk,
        webdav=webdav,
        settings=_settings(),
        workspace=tmp_path,
        run_id=44,
        should_stop=lambda: len(webdav.uploaded) >= 1,
    )

    assert result["stopped"] is True
    assert result["copied"] == 1
    assert len(yadisk.files) == 2


def test_transfer_fails_closed_when_webdav_stat_is_unavailable(tmp_path: Path) -> None:
    source = "/source/cartoons/Program/S01/S01E01.mkv"
    yadisk = _FakeYaDisk({source: b"video-data"})
    webdav = _FakeWebDav()
    webdav.stat_error = RuntimeError("service unavailable")
    db = _FakeDb()

    with pytest.raises(RuntimeError, match="service unavailable"):
        run_transfer(
            db=db,
            yadisk=yadisk,
            webdav=webdav,
            settings=_settings(),
            workspace=tmp_path,
            run_id=45,
            should_stop=lambda: False,
        )

    assert db.rows == {}
    assert webdav.uploaded == []
    assert yadisk.removed == []


def test_transfer_never_deletes_source_after_discovery(tmp_path: Path) -> None:
    source = "/source/cartoons/Program/S01/S01E01.mkv"
    yadisk = _FakeYaDisk({source: b"original-video"})
    yadisk.metadata_override[source] = b"replacement-video"
    webdav = _FakeWebDav()
    db = _FakeDb()

    result = run_transfer(
        db=db,
        yadisk=yadisk,
        webdav=webdav,
        settings=_settings(),
        workspace=tmp_path,
        run_id=46,
        should_stop=lambda: False,
    )

    assert result["copied"] == 1
    assert result["failed"] == 0
    assert len(webdav.uploaded) == 1
    assert yadisk.removed == []
    assert source in yadisk.files
    assert db.rows[source]["status"] == "uploaded"


def test_transfer_second_run_does_not_upload_copied_video_again(tmp_path: Path) -> None:
    source = "/source/cartoons/Program/S01/S01E01.mkv"
    yadisk = _FakeYaDisk({source: b"video-data"})
    webdav = _FakeWebDav()
    db = _FakeDb()

    first = run_transfer(
        db=db,
        yadisk=yadisk,
        webdav=webdav,
        settings=_settings(),
        workspace=tmp_path,
        run_id=47,
        should_stop=lambda: False,
    )
    second = run_transfer(
        db=db,
        yadisk=yadisk,
        webdav=webdav,
        settings=_settings(),
        workspace=tmp_path,
        run_id=48,
        should_stop=lambda: False,
    )

    assert first["copied"] == 1
    assert second["considered"] == 0
    assert len(webdav.uploaded) == 1
    assert source in yadisk.files


def test_transfer_keeps_historical_moved_checkpoint_terminal(
    tmp_path: Path,
) -> None:
    source = "/source/shows/Program/S01/S01E01.mkv"
    content = b"video-data"
    source_md5 = hashlib.md5(content).hexdigest()  # noqa: S324
    target = "/Manzara/Shayan/shows/Program/S01/S01E01.mkv"
    yadisk = _FakeYaDisk({})
    webdav = _FakeWebDav()
    webdav.files[target] = content
    webdav.checksums[target] = source_md5
    db = _FakeDb()
    db.rows[source] = {
        "source_path": source,
        "category": "shows",
        "source_md5": source_md5,
        "source_size": len(content),
        "target_path": target,
        "status": "moved",
        "target_etag": None,
        "target_checksum": source_md5,
    }

    result = run_transfer(
        db=db,
        yadisk=yadisk,
        webdav=webdav,
        settings=_settings(),
        workspace=tmp_path,
        run_id=49,
        should_stop=lambda: False,
    )

    assert result["copied"] == 0
    assert result["considered"] == 0
    assert webdav.uploaded == []
    assert db.rows[source]["status"] == "moved"
