"""Tests for resumable document synchronization to primary S3 storage."""

from __future__ import annotations

import hashlib
import base64
from pathlib import Path

from app.document_storage import DocumentStorageSettings, S3ConnectionSettings
from app.modules.maintenance.runtime.sync_documents_s3 import (
    _result_exit_code,
    _validate_primary_buckets,
    run_document_sync,
)


class FakeYaDisk:
    def __init__(
        self,
        files: dict[str, bytes],
        *,
        include_public_metadata: bool = True,
    ) -> None:
        self.files = dict(files)
        self.downloaded: list[str] = []
        self.include_public_metadata = include_public_metadata
        self.published: list[str] = []

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
                    "public_key": "key:" + first if self.include_public_metadata else "",
                    "public_url": (
                        "https://disk.example/" + first
                        if self.include_public_metadata
                        else ""
                    ),
                }
        return iter(direct.values())

    def download(self, source_path, target_path):  # noqa: ANN001
        self.downloaded.append(str(source_path))
        Path(target_path).write_bytes(self.files[str(source_path)])

    def publish(self, source_path):  # noqa: ANN001
        self.published.append(str(source_path))

    def get_meta(self, source_path, **_kwargs):  # noqa: ANN001
        name = Path(str(source_path)).name
        return {
            "public_key": "published:" + name,
            "public_url": "https://disk.example/published/" + name,
        }


class StreamingYaDisk(FakeYaDisk):
    def __init__(self, primary_s3: "FakeS3") -> None:
        super().__init__(
            {
                "/documents/first.pdf": b"first",
                "/documents/later/second.pdf": b"second",
            }
        )
        self.primary_s3 = primary_s3

    def listdir(self, path, **kwargs):  # noqa: ANN001
        if str(path) == "/documents/later":
            assert self.primary_s3.uploads, "first file must upload before later discovery"
        return super().listdir(path, **kwargs)


class FakeS3:
    def __init__(self, *, corrupt_upload: bool = False) -> None:
        self.objects: dict[tuple[str, str], dict] = {}
        self.uploads: list[tuple[str, str]] = []
        self.downloads: list[tuple[str, str]] = []
        self.deletes: list[tuple[str, str]] = []
        self.incomplete_uploads: dict[tuple[str, str], list[str]] = {}
        self.aborted_uploads: list[tuple[str, str, str]] = []
        self.corrupt_upload = corrupt_upload
        self.bucket_acls: dict[str, dict] = {}

    def inventory(self, bucket: str) -> dict[str, dict]:
        return {
            key: dict(value)
            for (item_bucket, key), value in self.objects.items()
            if item_bucket == bucket
        }

    def head_bucket(self, *, Bucket):  # noqa: N803, ANN001
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def get_bucket_acl(self, *, Bucket):  # noqa: N803, ANN001
        return self.bucket_acls[str(Bucket)]

    def head_object(self, *, Bucket, Key):  # noqa: N803, ANN001
        return dict(self.objects[(str(Bucket), str(Key))])

    def upload_file(
        self,
        filename,
        bucket,
        key,
        ExtraArgs,
        Callback=None,  # noqa: N803, ANN001
    ):
        content = Path(filename).read_bytes()
        stored_content = (
            bytes([content[0] ^ 1]) + content[1:]
            if self.corrupt_upload and content
            else content
        )
        etag = hashlib.md5(content).hexdigest()  # noqa: S324
        self.objects[(str(bucket), str(key))] = {
            "ContentLength": len(stored_content),
            "ETag": etag,
            "Metadata": dict(ExtraArgs.get("Metadata") or {}),
            "Body": stored_content,
        }
        self.uploads.append((str(bucket), str(key)))
        if Callback is not None:
            Callback(len(content))

    def download_file(self, bucket, key, filename):  # noqa: ANN001
        self.downloads.append((str(bucket), str(key)))
        Path(filename).write_bytes(self.objects[(str(bucket), str(key))]["Body"])

    def delete_object(self, *, Bucket, Key):  # noqa: N803, ANN001
        self.deletes.append((str(Bucket), str(Key)))
        self.objects.pop((str(Bucket), str(Key)), None)

    def list_multipart_uploads(self, *, Bucket, Prefix, **_kwargs):  # noqa: N803, ANN001
        uploads = [
            {"Key": key, "UploadId": upload_id}
            for (bucket, key), upload_ids in self.incomplete_uploads.items()
            if bucket == str(Bucket) and key.startswith(str(Prefix))
            for upload_id in upload_ids
        ]
        return {"Uploads": uploads, "IsTruncated": False}

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):  # noqa: N803, ANN001
        identity = (str(Bucket), str(Key))
        self.aborted_uploads.append((*identity, str(UploadId)))
        self.incomplete_uploads[identity].remove(str(UploadId))


class FakeRepository:
    def __init__(self, documents: dict[str, dict] | None = None) -> None:
        self.documents = documents or {}
        self.saved: list[dict] = []
        self.upstream_calls: list[tuple[object, str, str]] = []

    def list_documents(self):
        return {key: dict(value) for key, value in self.documents.items()}

    def save_verified_document(self, payload):  # noqa: ANN001
        item = dict(payload)
        self.documents[item["md5"]] = item
        self.saved.append(item)
        return bool(item.get("created"))

    def list_upstream_metadata(self, s3, bucket, endpoint_url):  # noqa: ANN001
        self.upstream_calls.append((s3, str(bucket), str(endpoint_url)))
        return {}


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
        primary=S3ConnectionSettings(
            endpoint_url="https://s3.primary.example.test",
            region_name="primary-region",
            access_key_id="primary-access",
            secret_access_key="primary-secret",
        ),
        legacy=S3ConnectionSettings(
            endpoint_url="https://s3.legacy.example.test",
            region_name="legacy-region",
            access_key_id="legacy-access",
            secret_access_key="legacy-secret",
        ),
        public_bucket="public-docs",
        private_bucket="private-docs",
        legacy_public_bucket="legacy-public-docs",
        legacy_private_bucket="legacy-private-docs",
        upstream_bucket="upstream",
        encryption_key=base64.urlsafe_b64encode(b"0" * 32).decode(),
    )


def test_primary_bucket_preflight_requires_public_and_private_policies() -> None:
    s3 = FakeS3()
    s3.bucket_acls = {
        "public-docs": {
            "Grants": [
                {
                    "Grantee": {
                        "Type": "Group",
                        "URI": "http://acs.amazonaws.com/groups/global/AllUsers",
                    },
                    "Permission": "READ",
                }
            ]
        },
        "private-docs": {"Grants": []},
    }

    _validate_primary_buckets(s3, "public-docs", "private-docs")


def test_primary_bucket_preflight_rejects_public_private_bucket() -> None:
    s3 = FakeS3()
    public_read = {
        "Grants": [
            {
                "Grantee": {
                    "Type": "Group",
                    "URI": "http://acs.amazonaws.com/groups/global/AllUsers",
                },
                "Permission": "READ",
            }
        ]
    }
    s3.bucket_acls = {
        "public-docs": public_read,
        "private-docs": public_read,
    }

    try:
        _validate_primary_buckets(s3, "public-docs", "private-docs")
    except RuntimeError as exc:
        assert "private bucket must not allow public read" in str(exc)
    else:
        raise AssertionError("public private bucket should fail validation")


def test_sync_prefers_valid_cache_and_uploads_missing_object(tmp_path: Path) -> None:
    content = b"pdf-content"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    (tmp_path / f"{digest}.pdf").write_bytes(content)
    yadisk = FakeYaDisk({"/documents/book.pdf": content})
    primary_s3 = FakeS3()
    primary_s3.incomplete_uploads[("public-docs", f"{digest}.pdf")] = [
        "interrupted-upload"
    ]
    legacy_s3 = FakeS3()
    repository = FakeRepository()
    state_db = FakeStateDb()

    result = run_document_sync(
        repository=repository,
        state_db=state_db,
        yadisk=yadisk,
        primary_s3=primary_s3,
        legacy_s3=legacy_s3,
        settings=settings(tmp_path),
        workspace=tmp_path / "work",
        run_id=7,
        should_stop=lambda: False,
    )

    assert result["uploaded"] == 1
    assert result["source_cache"] == 1
    assert yadisk.downloaded == []
    assert primary_s3.uploads == [("public-docs", f"{digest}.pdf")]
    assert primary_s3.aborted_uploads == [
        ("public-docs", f"{digest}.pdf", "interrupted-upload")
    ]
    assert primary_s3.downloads == [("public-docs", f"{digest}.pdf")]
    assert repository.saved[-1]["primary_storage_verified_at"]
    assert state_db.events[-1]["type"] == "task.progress"
    assert any(
        event["payload"]["progress"]["bytes_completed"] > 0
        for event in state_db.events
        if event["type"] == "task.progress"
    )
    assert any(
        event["payload"]["progress"]["current"] == 0
        and event["payload"]["progress"]["bytes_completed"] > 0
        and event["payload"]["progress"]["percent"] == 0
        and event["payload"]["progress"]["stage"] == "streaming"
        for event in state_db.events
        if event["type"] == "task.progress"
    )
    assert repository.upstream_calls == [
        (legacy_s3, "upstream", "https://s3.legacy.example.test")
    ]

    saved_count = len(repository.saved)
    second = run_document_sync(
        repository=repository,
        state_db=state_db,
        yadisk=yadisk,
        primary_s3=primary_s3,
        legacy_s3=legacy_s3,
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
    primary_s3 = FakeS3()
    primary_s3.objects[("public-docs", key)] = {
        "ContentLength": len(content),
        "ETag": digest,
        "Metadata": {},
    }

    result = run_document_sync(
        repository=FakeRepository(),
        state_db=FakeStateDb(),
        yadisk=yadisk,
        primary_s3=primary_s3,
        legacy_s3=FakeS3(),
        settings=settings(tmp_path),
        workspace=tmp_path / "work",
        run_id=8,
        should_stop=lambda: False,
    )

    assert result["verified"] == 1
    assert result["uploaded"] == 0
    assert primary_s3.downloads == []
    assert yadisk.downloaded == []


def test_restricted_sync_copies_to_private_then_removes_public(tmp_path: Path) -> None:
    content = b"restricted"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    key = f"{digest}.pdf"
    cache = tmp_path / f"{digest}.pdf"
    cache.write_bytes(content)
    yadisk = FakeYaDisk({"/documents/private/book.pdf": content})
    primary_s3 = FakeS3()
    primary_s3.objects[("public-docs", key)] = {
        "ContentLength": len(content),
        "ETag": digest,
        "Metadata": {},
        "Body": content,
    }
    repository = FakeRepository(
        {
            digest: {
                "md5": digest,
                "document_url": (
                    f"https://s3.primary.example.test/public-docs/{key}"
                ),
                "sharing_restricted": True,
            }
        }
    )

    result = run_document_sync(
        repository=repository,
        state_db=FakeStateDb(),
        yadisk=yadisk,
        primary_s3=primary_s3,
        legacy_s3=FakeS3(),
        settings=settings(tmp_path),
        workspace=tmp_path / "work",
        run_id=9,
        should_stop=lambda: False,
    )

    assert primary_s3.uploads == [("private-docs", key)]
    assert primary_s3.deletes == [("public-docs", key)]
    assert result["private_cleaned"] == 1
    assert str(repository.saved[-1]["document_url"]).startswith("enc:")


def test_restricted_sync_uses_legacy_s3_before_yandex(tmp_path: Path) -> None:
    content = b"restricted-from-s3"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    key = f"{digest}.pdf"
    yadisk = FakeYaDisk({"/documents/private/book.pdf": content})
    primary_s3 = FakeS3()
    legacy_s3 = FakeS3()
    legacy_s3.objects[("legacy-public-docs", key)] = {
        "ContentLength": len(content),
        "ETag": digest,
        "Metadata": {},
        "Body": content,
    }
    repository = FakeRepository(
        {
            digest: {
                "md5": digest,
                "document_url": (
                    f"https://s3.legacy.example.test/legacy-public-docs/{key}"
                ),
                "sharing_restricted": True,
            }
        }
    )

    result = run_document_sync(
        repository=repository,
        state_db=FakeStateDb(),
        yadisk=yadisk,
        primary_s3=primary_s3,
        legacy_s3=legacy_s3,
        settings=settings(tmp_path),
        workspace=tmp_path / "work",
        run_id=10,
        should_stop=lambda: False,
    )

    assert result["source_legacy_s3"] == 1
    assert result["source_yandex"] == 0
    assert legacy_s3.downloads == [("legacy-public-docs", key)]
    assert primary_s3.uploads == [("private-docs", key)]
    assert legacy_s3.deletes == [("legacy-public-docs", key)]
    assert yadisk.downloaded == []


def test_sync_rejects_corrupt_remote_readback(tmp_path: Path) -> None:
    content = b"correct-document"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    (tmp_path / f"{digest}.pdf").write_bytes(content)
    repository = FakeRepository()

    result = run_document_sync(
        repository=repository,
        state_db=FakeStateDb(),
        yadisk=FakeYaDisk({"/documents/book.pdf": content}),
        primary_s3=FakeS3(corrupt_upload=True),
        legacy_s3=FakeS3(),
        settings=settings(tmp_path),
        workspace=tmp_path / "work",
        run_id=12,
        should_stop=lambda: False,
    )

    assert result["failed"] == 1
    assert repository.saved == []


def test_sync_never_publishes_restricted_yandex_document(tmp_path: Path) -> None:
    content = b"restricted-without-public-link"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    (tmp_path / f"{digest}.pdf").write_bytes(content)
    yadisk = FakeYaDisk(
        {"/documents/private/book.pdf": content},
        include_public_metadata=False,
    )
    repository = FakeRepository()

    result = run_document_sync(
        repository=repository,
        state_db=FakeStateDb(),
        yadisk=yadisk,
        primary_s3=FakeS3(),
        legacy_s3=FakeS3(),
        settings=settings(tmp_path),
        workspace=tmp_path / "work",
        run_id=13,
        should_stop=lambda: False,
    )

    assert result["failed"] == 0
    assert yadisk.published == []
    assert repository.saved[-1]["ya_public_url"] is None


def test_sync_graceful_stop_finishes_current_document(tmp_path: Path) -> None:
    files = {
        "/documents/one.pdf": b"one",
        "/documents/two.pdf": b"two",
    }
    primary_s3 = FakeS3()

    result = run_document_sync(
        repository=FakeRepository(),
        state_db=FakeStateDb(),
        yadisk=FakeYaDisk(files),
        primary_s3=primary_s3,
        legacy_s3=FakeS3(),
        settings=settings(tmp_path),
        workspace=tmp_path / "work",
        run_id=14,
        should_stop=lambda: len(primary_s3.uploads) >= 1,
    )

    assert result["stopped"] is True
    assert result["processed"] == 1
    assert len(primary_s3.uploads) == 1


def test_sync_reports_source_database_reconciliation_without_failing(
    tmp_path: Path,
) -> None:
    source_content = b"source-document"
    source_md5 = hashlib.md5(source_content).hexdigest()  # noqa: S324
    database_only_md5 = hashlib.md5(b"database-only").hexdigest()  # noqa: S324
    (tmp_path / f"{source_md5}.pdf").write_bytes(source_content)
    repository = FakeRepository(
        {
            database_only_md5: {
                "md5": database_only_md5,
                "document_url": (
                    "https://s3.legacy.example.test/legacy-public-docs/"
                    f"{database_only_md5}.pdf"
                ),
            }
        }
    )

    result = run_document_sync(
        repository=repository,
        state_db=FakeStateDb(),
        yadisk=FakeYaDisk({"/documents/book.pdf": source_content}),
        primary_s3=FakeS3(corrupt_upload=True),
        legacy_s3=FakeS3(),
        settings=settings(tmp_path),
        workspace=tmp_path / "work",
        run_id=15,
        should_stop=lambda: False,
    )

    assert result["source_files"] == 1
    assert result["source_documents"] == 1
    assert result["database_rows_before"] == 1
    assert result["database_rows_after"] == 1
    assert result["synced_source_documents"] == 0
    assert result["unsynced_source_documents"] == 1
    assert result["database_only_rows"] == 1
    assert result["fully_synced"] is False
    assert result["failed"] == 1
    assert _result_exit_code(result) == 0


def test_sync_reports_complete_reconciliation(tmp_path: Path) -> None:
    content = b"complete-document"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    (tmp_path / f"{digest}.pdf").write_bytes(content)

    result = run_document_sync(
        repository=FakeRepository(),
        state_db=FakeStateDb(),
        yadisk=FakeYaDisk({"/documents/book.pdf": content}),
        primary_s3=FakeS3(),
        legacy_s3=FakeS3(),
        settings=settings(tmp_path),
        workspace=tmp_path / "work",
        run_id=16,
        should_stop=lambda: False,
    )

    assert result["source_files"] == 1
    assert result["source_documents"] == 1
    assert result["database_rows_before"] == 0
    assert result["database_rows_after"] == 1
    assert result["synced_source_documents"] == 1
    assert result["unsynced_source_documents"] == 0
    assert result["database_only_rows"] == 0
    assert result["fully_synced"] is True
    assert _result_exit_code(result) == 0


def test_sync_uploads_each_document_before_discovering_later_directories(
    tmp_path: Path,
) -> None:
    primary_s3 = FakeS3()

    result = run_document_sync(
        repository=FakeRepository(),
        state_db=FakeStateDb(),
        yadisk=StreamingYaDisk(primary_s3),
        primary_s3=primary_s3,
        legacy_s3=FakeS3(),
        settings=settings(tmp_path),
        workspace=tmp_path / "work",
        run_id=17,
        should_stop=lambda: False,
    )

    assert result["discovery_complete"] is True
    assert result["source_files"] == 2
    assert result["source_documents"] == 2
    assert result["synced_source_documents"] == 2


def test_stopped_streaming_discovery_does_not_report_database_only_rows(
    tmp_path: Path,
) -> None:
    primary_s3 = FakeS3()
    orphan_md5 = hashlib.md5(b"orphan").hexdigest()  # noqa: S324

    result = run_document_sync(
        repository=FakeRepository({orphan_md5: {"md5": orphan_md5}}),
        state_db=FakeStateDb(),
        yadisk=StreamingYaDisk(primary_s3),
        primary_s3=primary_s3,
        legacy_s3=FakeS3(),
        settings=settings(tmp_path),
        workspace=tmp_path / "work",
        run_id=18,
        should_stop=lambda: bool(primary_s3.uploads),
    )

    assert result["stopped"] is True
    assert result["discovery_complete"] is False
    assert result["source_files"] == 1
    assert result["database_only_rows"] is None
    assert result["fully_synced"] is False
