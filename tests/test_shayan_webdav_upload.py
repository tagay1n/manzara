"""Tests for direct Shayan uploads from local storage to Hetzner WebDAV."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.modules.shayan.runtime.transfer_yadisk_webdav import (
    NextcloudSettings,
    RemoteFile,
    temporary_remote_path,
)
from app.modules.shayan.runtime.upload_webdav import run_upload


class _FakeDb:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = [dict(row) for row in rows]
        self.started: list[dict] = []
        self.uploaded: list[dict] = []
        self.failed: list[dict] = []
        self.progress: list[dict] = []
        self.events: list[dict] = []

    def list_shayan_manifest_webdav_upload_candidates(self, *, limit=500):  # noqa: ANN001
        _ = limit
        return [dict(row) for row in self.rows]

    def mark_shayan_manifest_webdav_upload_started(self, entry_key, **values):  # noqa: ANN001, ANN003
        self.started.append({"entry_key": entry_key, **values})
        return 1

    def mark_shayan_manifest_webdav_uploaded(self, entry_key, **values):  # noqa: ANN001, ANN003
        self.uploaded.append({"entry_key": entry_key, **values})
        return 1

    def mark_shayan_manifest_webdav_failed(self, entry_key, *, error_text):  # noqa: ANN001
        self.failed.append({"entry_key": entry_key, "error_text": error_text})
        return 1

    def update_run_progress(self, _run_id, progress):  # noqa: ANN001
        self.progress.append(dict(progress))

    def insert_event(self, _event_type, **values):  # noqa: ANN001, ANN003
        self.events.append(dict(values))
        return 1


class _FakeWebDav:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.uploaded: list[str] = []
        self.moves: list[tuple[str, str]] = []
        self.preflighted: list[str] = []

    def stat(self, path: str):  # noqa: ANN201
        if path.startswith("/Hetzner/") and path.count("/") == 2:
            self.preflighted.append(path)
            return RemoteFile(path=path, size=0, etag="dir", md5=None, is_directory=True)
        content = self.files.get(path)
        if content is None:
            return None
        return RemoteFile(path=path, size=len(content), etag="etag-1", md5=None)

    def stream_md5(self, path: str) -> str:
        return hashlib.md5(self.files[path]).hexdigest()  # noqa: S324

    def ensure_directory(self, _path: str) -> None:
        return None

    def upload(self, local_path: Path, path: str, *, md5: str, on_progress=None):  # noqa: ANN001, ANN201
        content = local_path.read_bytes()
        self.files[path] = content
        self.uploaded.append(path)
        if on_progress is not None:
            on_progress(len(content), len(content))
        return RemoteFile(path=path, size=len(content), etag="staged", md5=md5)

    def move(self, source: str, target: str, *, overwrite: bool) -> None:
        assert overwrite is True
        self.files[target] = self.files.pop(source)
        self.moves.append((source, target))

    def delete(self, path: str) -> None:
        self.files.pop(path, None)


def _settings() -> NextcloudSettings:
    return NextcloudSettings(
        webdav_url="https://cloud.example/remote.php/dav/files/Admin",
        username="Admin",
        password="password",
        target_dirs={
            "cartoons": "/Hetzner/Cartoons",
            "shows": "/Hetzner/Shows",
        },
    )


def test_upload_sends_local_video_to_category_target_and_deletes_after_verification(
    tmp_path: Path,
) -> None:
    local_path = tmp_path / "videos" / "cartoons" / "Show" / "S01" / "S01E01.mkv"
    local_path.parent.mkdir(parents=True)
    local_path.write_bytes(b"video-data")
    db = _FakeDb(
        [
            {
                "entry_key": "ep-1",
                "payload_hash": "payload-1",
                "payload": {"file": str(local_path), "category": "cartoons"},
            }
        ]
    )
    webdav = _FakeWebDav()

    summary = run_upload(
        db=db,
        webdav=webdav,
        settings=_settings(),
        output_path=tmp_path,
        run_id=71,
        should_stop=lambda: False,
    )

    target = "/Hetzner/Cartoons/Show/S01/S01E01.mkv"
    source_md5 = hashlib.md5(b"video-data").hexdigest()  # noqa: S324
    assert webdav.uploaded == [temporary_remote_path(target, source_md5)]
    assert webdav.moves == [(temporary_remote_path(target, source_md5), target)]
    assert webdav.files[target] == b"video-data"
    assert local_path.exists() is False
    assert db.started[0]["remote_path"] == target
    assert db.uploaded[0]["remote_path"] == target
    assert db.uploaded[0]["target_checksum"] == source_md5
    assert summary["kind"] == "shayan.webdav_upload_summary"
    assert summary["uploaded"] == 1
    assert summary["deleted_local"] == 1
    assert summary["failed"] == 0
    assert db.progress[-1]["percent"] == 100
    assert db.events[-1]["task_id"] == "shayan.upload_yadisk"


def test_upload_reuses_verified_final_file_without_reuploading(tmp_path: Path) -> None:
    local_path = tmp_path / "videos" / "shows" / "Show" / "episode.mp4"
    local_path.parent.mkdir(parents=True)
    local_path.write_bytes(b"existing-video")
    target = "/Hetzner/Shows/Show/episode.mp4"
    db = _FakeDb(
        [
            {
                "entry_key": "ep-2",
                "payload_hash": "payload-2",
                "payload": {"file": str(local_path), "category": "shows"},
            }
        ]
    )
    webdav = _FakeWebDav()
    webdav.files[target] = b"existing-video"

    summary = run_upload(
        db=db,
        webdav=webdav,
        settings=_settings(),
        output_path=tmp_path,
        run_id=72,
        should_stop=lambda: False,
    )

    assert webdav.uploaded == []
    assert summary["uploaded"] == 1
    assert summary["reused"] == 1
    assert local_path.exists() is False


def test_upload_recovers_verified_final_after_local_delete(tmp_path: Path) -> None:
    local_path = tmp_path / "videos" / "shows" / "Show" / "episode.mp4"
    target = "/Hetzner/Shows/Show/episode.mp4"
    content = b"already-uploaded"
    source_md5 = hashlib.md5(content).hexdigest()  # noqa: S324
    db = _FakeDb(
        [
            {
                "entry_key": "ep-3",
                "payload_hash": "payload-3",
                "payload": {"file": str(local_path), "category": "shows"},
                "source_md5": source_md5,
                "source_size": len(content),
                "target_path": target,
                "target_etag": None,
                "target_checksum": None,
            }
        ]
    )
    webdav = _FakeWebDav()
    webdav.files[target] = content

    summary = run_upload(
        db=db,
        webdav=webdav,
        settings=_settings(),
        output_path=tmp_path,
        run_id=73,
        should_stop=lambda: False,
    )

    assert summary["uploaded"] == 1
    assert summary["reused"] == 1
    assert summary["missing_local"] == 0
    assert db.started == []
    assert db.uploaded[0]["remote_path"] == target
