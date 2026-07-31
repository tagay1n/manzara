"""Tests for resumable document synchronization to primary S3 storage."""

from __future__ import annotations

import hashlib
import base64
from pathlib import Path

from app.document_storage import DocumentStorageSettings
from app.modules.maintenance.runtime.sync_documents_s3 import run_document_sync


class FakeYaDisk:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.downloaded: list[str] = []

    def listdir(self, path, **_kwargs):  # noqa: ANN001
        prefix = str(path).rstrip("/") + "/"
        direct = {}
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
                    "mime_type": "application/pdf",
                    "resource_id": "resource:" + first,
                    "public_key": "key:" + first,
                    "public_url": "https://disk.example/" + first,
                }
        return iter(direct.values())

    def download(self, source_path, target_path):  # noqa: ANN001
        self.downloaded.append(str(source_path))
        Path(target_path).write_bytes(self.files[str(source_path)])


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict] = {}
        self.uploads: list[tuple[str, str]] = []
        self.downloads: list[tuple[str, str]] = []
        self.deletes: list[tuple[str, str]] = []

    def inventory(self, bucket: str) -> dict[str, dict]:
        return {
            key: dict(value)
            for (item_bucket, key), value in self.objects.items()
            if item_bucket == bucket
        }

    def head_object(self, *, Bucket, Key):  # noqa: N803, ANN001
        return dict(self.objects[(str(Bucket), str(Key))])

    def upload_file(self, filename, bucket, key, ExtraArgs):  # noqa: N803, ANN001
        content = Path(filename).read_bytes()
        etag = hashlib.md5(content).hexdigest()  # noqa: S324
        self.objects[(str(bucket), str(key))] = {
            "ContentLength": len(content),
            "ETag": etag,
            "Metadata": dict(ExtraArgs.get("Metadata") or {}),
        }
        self.uploads.append((str(bucket), str(key)))

    def download_file(self, bucket, key, filename):  # noqa: ANN001
        self.downloads.append((str(bucket), str(key)))
        Path(filename).write_bytes(self.objects[(str(bucket), str(key))]["Body"])

    def delete_object(self, *, Bucket, Key):  # noqa: N803, ANN001
        self.deletes.append((str(Bucket), str(Key)))
        self.objects.pop((str(Bucket), str(Key)), None)


class FakeRepository:
    def __init__(self, documents: dict[str, dict] | None = None) -> None:
        self.documents = documents or {}
        self.saved: list[dict] = []

    def list_documents(self):
        return {key: dict(value) for key, value in self.documents.items()}

    def save_verified_document(self, payload):  # noqa: ANN001
        item = dict(payload)
        self.documents[item["md5"]] = item
        self.saved.append(item)
        return bool(item.get("created"))


class FakeStateDb:
    def __init__(self) -> None:
        self.progress = []
        self.events = []

    def update_run_progress(self, run_id, payload):  # noqa: ANN001
        self.progress.append(dict(payload))

    def insert_event(self, event_type, **kwargs):  # noqa: ANN001, ANN003
        self.events.append({"type": event_type, **kwargs})


def settings(cache: Path) -> DocumentStorageSettings:
    return DocumentStorageSettings(
        cache_path=cache,
        source_path="/documents",
        restricted_path="/documents/private",
        endpoint_url="https://s3.example.test",
        region_name="region",
        public_bucket="public-docs",
        private_bucket="private-docs",
        upstream_bucket="upstream",
        access_key_id="access",
        secret_access_key="secret",
        encryption_key=base64.urlsafe_b64encode(b"0" * 32).decode(),
    )


def test_sync_prefers_valid_cache_and_uploads_missing_object(tmp_path: Path) -> None:
    content = b"pdf-content"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    (tmp_path / f"{digest}.pdf").write_bytes(content)
    yadisk = FakeYaDisk({"/documents/book.pdf": content})
    s3 = FakeS3()
    repository = FakeRepository()
    state_db = FakeStateDb()

    result = run_document_sync(
        repository=repository,
        state_db=state_db,
        yadisk=yadisk,
        s3=s3,
        settings=settings(tmp_path),
        workspace=tmp_path / "work",
        run_id=7,
        should_stop=lambda: False,
    )

    assert result["uploaded"] == 1
    assert result["source_cache"] == 1
    assert yadisk.downloaded == []
    assert s3.uploads == [("public-docs", f"{digest}.pdf")]
    assert repository.saved[-1]["primary_storage_verified_at"]
    assert state_db.events[-1]["type"] == "task.progress"

    saved_count = len(repository.saved)
    second = run_document_sync(
        repository=repository,
        state_db=state_db,
        yadisk=yadisk,
        s3=s3,
        settings=settings(tmp_path),
        workspace=tmp_path / "work-2",
        run_id=11,
        should_stop=lambda: False,
    )
    assert second["unchanged"] == 1
    assert len(repository.saved) == saved_count


def test_sync_reuses_plain_md5_etag_without_downloading(tmp_path: Path) -> None:
    content = b"already-stored"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    key = f"{digest}.pdf"
    yadisk = FakeYaDisk({"/documents/book.pdf": content})
    s3 = FakeS3()
    s3.objects[("public-docs", key)] = {
        "ContentLength": len(content),
        "ETag": digest,
        "Metadata": {},
    }

    result = run_document_sync(
        repository=FakeRepository(),
        state_db=FakeStateDb(),
        yadisk=yadisk,
        s3=s3,
        settings=settings(tmp_path),
        workspace=tmp_path / "work",
        run_id=8,
        should_stop=lambda: False,
    )

    assert result["verified"] == 1
    assert result["uploaded"] == 0
    assert s3.downloads == []
    assert yadisk.downloaded == []


def test_restricted_sync_copies_to_private_then_removes_public(tmp_path: Path) -> None:
    content = b"restricted"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    key = f"{digest}.pdf"
    cache = tmp_path / f"{digest}.pdf"
    cache.write_bytes(content)
    yadisk = FakeYaDisk({"/documents/private/book.pdf": content})
    s3 = FakeS3()
    s3.objects[("public-docs", key)] = {
        "ContentLength": len(content),
        "ETag": digest,
        "Metadata": {},
        "Body": content,
    }
    repository = FakeRepository(
        {
            digest: {
                "md5": digest,
                "document_url": f"https://s3.example.test/public-docs/{key}",
                "sharing_restricted": True,
            }
        }
    )

    result = run_document_sync(
        repository=repository,
        state_db=FakeStateDb(),
        yadisk=yadisk,
        s3=s3,
        settings=settings(tmp_path),
        workspace=tmp_path / "work",
        run_id=9,
        should_stop=lambda: False,
    )

    assert s3.uploads == [("private-docs", key)]
    assert s3.deletes == [("public-docs", key)]
    assert result["private_cleaned"] == 1
    assert str(repository.saved[-1]["document_url"]).startswith("enc:")


def test_restricted_sync_uses_existing_public_s3_before_yandex(tmp_path: Path) -> None:
    content = b"restricted-from-s3"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    key = f"{digest}.pdf"
    yadisk = FakeYaDisk({"/documents/private/book.pdf": content})
    s3 = FakeS3()
    s3.objects[("public-docs", key)] = {
        "ContentLength": len(content),
        "ETag": digest,
        "Metadata": {},
        "Body": content,
    }
    repository = FakeRepository(
        {
            digest: {
                "md5": digest,
                "document_url": f"https://s3.example.test/public-docs/{key}",
                "sharing_restricted": True,
            }
        }
    )

    result = run_document_sync(
        repository=repository,
        state_db=FakeStateDb(),
        yadisk=yadisk,
        s3=s3,
        settings=settings(tmp_path),
        workspace=tmp_path / "work",
        run_id=10,
        should_stop=lambda: False,
    )

    assert result["source_s3"] == 1
    assert result["source_yandex"] == 0
    assert s3.downloads == [("public-docs", key)]
    assert yadisk.downloaded == []
