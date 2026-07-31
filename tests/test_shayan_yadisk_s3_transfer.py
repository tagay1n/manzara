"""Tests for resumable Shayan Yandex Disk to S3 transfers."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.modules.shayan.runtime.transfer_yadisk_s3 import (
    S3Target,
    TransferSettings,
    destination_key,
    load_transfer_settings,
    run_transfer,
)


class _FakeYaDisk:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.removed: list[str] = []

    def listdir(self, path, **_kwargs):  # noqa: ANN001
        prefix = str(path).rstrip("/") + "/"
        direct: dict[str, dict] = {}
        for remote_path, content in self.files.items():
            if not remote_path.startswith(prefix):
                continue
            remainder = remote_path[len(prefix) :]
            first, _, tail = remainder.partition("/")
            child_path = prefix + first
            if tail:
                direct[first] = {"name": first, "path": child_path, "type": "dir"}
            else:
                direct[first] = {
                    "name": first,
                    "path": remote_path,
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
        return {
            "path": str(source_path),
            "type": "file",
            "size": len(content),
            "md5": hashlib.md5(content).hexdigest(),  # noqa: S324
        }

    def remove(self, source_path, **_kwargs):  # noqa: ANN001
        self.removed.append(str(source_path))
        self.files.pop(str(source_path), None)


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict] = {}
        self.uploaded: list[tuple[str, str]] = []

    def upload_file(self, filename, bucket, key, ExtraArgs):  # noqa: N803, ANN001
        content = Path(filename).read_bytes()
        self.objects[(str(bucket), str(key))] = {
            "ContentLength": len(content),
            "Metadata": dict(ExtraArgs.get("Metadata") or {}),
        }
        self.uploaded.append((str(bucket), str(key)))

    def head_object(self, *, Bucket, Key):  # noqa: N803, ANN001
        item = self.objects.get((str(Bucket), str(Key)))
        if item is None:
            raise RuntimeError("not found")
        return dict(item)


class _FakeDb:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.progress: list[dict] = []
        self.events: list[dict] = []

    def upsert_shayan_s3_transfer(self, **item):  # noqa: ANN003
        source_path = str(item["source_path"])
        previous = self.rows.get(source_path, {})
        same_source = (
            str(previous.get("source_md5") or "") == str(item.get("source_md5") or "")
            and int(previous.get("source_size") or 0)
            == int(item.get("source_size") or 0)
            and str(previous.get("target_bucket") or "")
            == str(item.get("target_bucket") or "")
            and str(previous.get("target_key") or "")
            == str(item.get("target_key") or "")
        )
        status = str(previous.get("status") or "pending") if same_source else "pending"
        self.rows[source_path] = {**previous, **item, "status": status}

    def list_shayan_s3_transfer_candidates(self):
        return [dict(row) for row in self.rows.values() if row.get("status") != "moved"]

    def mark_shayan_s3_transfer_state(self, source_path, *, status, error_text=None):  # noqa: ANN001
        self.rows[str(source_path)]["status"] = str(status)
        self.rows[str(source_path)]["error_text"] = error_text

    def update_run_progress(self, run_id, payload):  # noqa: ANN001
        self.progress.append({"run_id": int(run_id), **dict(payload)})

    def insert_event(self, event_type, **kwargs):  # noqa: ANN001, ANN003
        self.events.append({"type": event_type, **kwargs})


def _settings() -> TransferSettings:
    return TransferSettings(
        yadisk_token="token",
        source_dirs={"cartoons": "/source/cartoons", "shows": "/source/shows"},
        target=S3Target(
            endpoint_url="https://s3.example.test",
            region_name="region-1",
            bucket="video-archive",
            prefix="shayan",
            access_key_id="access",
            secret_access_key="secret",
        ),
    )


def test_load_transfer_settings_requires_new_object_storage_contract() -> None:
    payload = {
        "yandex": {
            "disk": {
                "oauth_token": "token",
                "shayan": {"cartoons": "/cartoons", "shows": "/shows"},
            }
        },
        "object_storage": {
            "shayan_archive": {
                "endpoint_url": "https://s3.example.test",
                "region_name": "region-1",
                "bucket": "videos",
                "prefix": "archive/shayan",
                "access_key_id": "access",
                "secret_access_key": "secret",
            }
        },
    }

    settings = load_transfer_settings(payload)
    assert settings.source_dirs["cartoons"] == "/cartoons"
    assert settings.target.bucket == "videos"
    assert settings.target.prefix == "archive/shayan"

    del payload["object_storage"]
    with pytest.raises(RuntimeError, match="object_storage.shayan_archive"):
        load_transfer_settings(payload)


def test_destination_key_preserves_category_hierarchy() -> None:
    assert (
        destination_key(
            source_root="/source/cartoons",
            source_path="/source/cartoons/Program/S01/S01E01.mkv",
            category="cartoons",
            prefix="archive/shayan",
        )
        == "archive/shayan/cartoons/Program/S01/S01E01.mkv"
    )


def test_transfer_verifies_s3_then_removes_yadisk_source_and_emits_progress(
    tmp_path: Path,
) -> None:
    source = "/source/cartoons/Program/S01/S01E01.mkv"
    yadisk = _FakeYaDisk({source: b"video-data"})
    s3 = _FakeS3()
    db = _FakeDb()

    result = run_transfer(
        db=db,
        yadisk=yadisk,
        s3=s3,
        settings=_settings(),
        workspace=tmp_path,
        run_id=41,
        should_stop=lambda: False,
    )

    target_key = "shayan/cartoons/Program/S01/S01E01.mkv"
    assert result["moved"] == 1
    assert result["failed"] == 0
    assert s3.uploaded == [("video-archive", target_key)]
    assert yadisk.removed == [source]
    assert db.rows[source]["status"] == "moved"
    assert db.progress[-1]["percent"] == 100
    assert db.events[-1]["type"] == "task.progress"


def test_transfer_resumes_from_verified_s3_without_uploading_again(
    tmp_path: Path,
) -> None:
    source = "/source/shows/Program/S01/S01E01.mkv"
    content = b"video-data"
    source_md5 = hashlib.md5(content).hexdigest()  # noqa: S324
    target_key = "shayan/shows/Program/S01/S01E01.mkv"
    yadisk = _FakeYaDisk({source: content})
    s3 = _FakeS3()
    s3.objects[("video-archive", target_key)] = {
        "ContentLength": len(content),
        "Metadata": {"source-md5": source_md5},
    }
    db = _FakeDb()

    result = run_transfer(
        db=db,
        yadisk=yadisk,
        s3=s3,
        settings=_settings(),
        workspace=tmp_path,
        run_id=42,
        should_stop=lambda: False,
    )

    assert result["moved"] == 1
    assert result["reused"] == 1
    assert s3.uploaded == []
    assert yadisk.removed == [source]


def test_transfer_stops_before_next_file_boundary(tmp_path: Path) -> None:
    files = {
        "/source/cartoons/A/S01/one.mkv": b"one",
        "/source/cartoons/A/S01/two.mkv": b"two",
    }
    yadisk = _FakeYaDisk(files)
    s3 = _FakeS3()
    db = _FakeDb()

    result = run_transfer(
        db=db,
        yadisk=yadisk,
        s3=s3,
        settings=_settings(),
        workspace=tmp_path,
        run_id=43,
        should_stop=lambda: len(s3.uploaded) >= 1,
    )

    assert result["stopped"] is True
    assert result["moved"] == 1
    assert len(yadisk.files) == 1
