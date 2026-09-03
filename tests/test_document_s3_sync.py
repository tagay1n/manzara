"""Tests for PostgreSQL-driven document uploads to primary S3 storage."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from app.document_storage import DocumentStorageSettings, S3ConnectionSettings
from app.modules.maintenance.runtime.sync_documents_s3 import (
    _result_exit_code,
    _validate_primary_buckets,
    run_document_upload,
)


class FakeYaDisk:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.downloaded: list[str] = []
        self.listdir_calls = 0
        self.published: list[str] = []

    def listdir(self, *_args, **_kwargs):
        self.listdir_calls += 1
        raise AssertionError("upload task must not traverse Yandex Disk")

    def publish(self, path):  # noqa: ANN001
        self.published.append(str(path))
        raise AssertionError("upload task must not publish Yandex documents")

    def download(self, source_path, target_path):  # noqa: ANN001
        self.downloaded.append(str(source_path))
        if str(source_path) not in self.files:
            raise FileNotFoundError(str(source_path))
        Path(target_path).write_bytes(self.files[str(source_path)])


class FakeS3:
    def __init__(self, *, omit_upload_metadata: bool = False) -> None:
        self.objects: dict[tuple[str, str], dict] = {}
        self.heads: list[tuple[str, str]] = []
        self.uploads: list[tuple[str, str]] = []
        self.deletes: list[tuple[str, str]] = []
        self.incomplete_uploads: dict[tuple[str, str], list[str]] = {}
        self.aborted_uploads: list[tuple[str, str, str]] = []
        self.bucket_acls: dict[str, dict] = {}
        self.omit_upload_metadata = omit_upload_metadata

    def head_bucket(self, *, Bucket):  # noqa: N803, ANN001
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def get_bucket_acl(self, *, Bucket):  # noqa: N803, ANN001
        return self.bucket_acls[str(Bucket)]

    def head_object(self, *, Bucket, Key):  # noqa: N803, ANN001
        identity = (str(Bucket), str(Key))
        self.heads.append(identity)
        if identity not in self.objects:
            raise KeyError(identity)
        return dict(self.objects[identity])

    def upload_file(
        self, filename, bucket, key, ExtraArgs, Callback=None  # noqa: N803, ANN001
    ):
        content = Path(filename).read_bytes()
        digest = hashlib.md5(content).hexdigest()  # noqa: S324
        self.objects[(str(bucket), str(key))] = {
            "ContentLength": len(content),
            "ETag": digest,
            "Metadata": (
                {} if self.omit_upload_metadata else dict(ExtraArgs.get("Metadata") or {})
            ),
            "Body": content,
        }
        self.uploads.append((str(bucket), str(key)))
        if Callback:
            Callback(len(content))

    def delete_object(self, *, Bucket, Key):  # noqa: N803, ANN001
        self.deletes.append((str(Bucket), str(Key)))
        self.objects.pop((str(Bucket), str(Key)), None)

    def list_multipart_uploads(self, *, Bucket, Prefix, **_kwargs):  # noqa: N803, ANN001
        uploads = [
            {"Key": key, "UploadId": upload_id}
            for (bucket, key), upload_ids in self.incomplete_uploads.items()
            if bucket == str(Bucket) and key == str(Prefix)
            for upload_id in upload_ids
        ]
        return {"Uploads": uploads, "IsTruncated": False}

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):  # noqa: N803, ANN001
        identity = (str(Bucket), str(Key))
        self.aborted_uploads.append((*identity, str(UploadId)))
        self.incomplete_uploads[identity].remove(str(UploadId))


class FakeRepository:
    def __init__(self, documents: list[dict], *, reject_checkpoint: bool = False) -> None:
        self.documents = [dict(document) for document in documents]
        self.saved: list[tuple[str, dict]] = []
        self.reject_checkpoint = reject_checkpoint

    def list_pending_documents(self):
        return [
            dict(document)
            for document in self.documents
            if not str(document.get("document_url") or "").strip()
        ]

    def count_pending_documents(self):
        return len(self.list_pending_documents())

    def save_storage_checkpoint(self, md5, payload, *, expected):  # noqa: ANN001
        matches = [document for document in self.documents if document["md5"] == md5]
        if len(matches) != 1:
            raise RuntimeError("ambiguous checkpoint")
        if self.reject_checkpoint:
            return False
        if str(matches[0].get("document_url") or "").strip():
            return False
        if any(matches[0].get(key) != expected.get(key) for key in expected):
            return False
        matches[0].update(dict(payload))
        self.saved.append((str(md5), dict(payload)))
        return True


class FakeStateDb:
    def __init__(self) -> None:
        self.progress: list[dict] = []

    def publish_run_progress(self, **kwargs):  # noqa: ANN003
        self.progress.append(dict(kwargs["progress"]))


def settings(cache: Path) -> DocumentStorageSettings:
    return DocumentStorageSettings(
        cache_path=cache,
        source_path="/documents",
        restricted_path="/documents/private",
        filtered_out_path="/documents/filtered-out",
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
        encryption_key=base64.urlsafe_b64encode(b"0" * 32).decode(),
    )


def document(
    content: bytes,
    *,
    path: str = "/documents/book.pdf",
    restricted: bool = False,
) -> dict:
    return {
        "md5": hashlib.md5(content).hexdigest(),  # noqa: S324
        "mime_type": "application/pdf",
        "ya_path": path,
        "sharing_restricted": restricted,
        "document_url": None,
        "primary_storage_size": None,
        "primary_storage_etag": None,
        "primary_storage_verified_at": None,
    }


def run_one(tmp_path: Path, row: dict, *, yadisk=None, primary_s3=None):
    repository = FakeRepository([row])
    yadisk = yadisk or FakeYaDisk({row["ya_path"]: b"document"})
    primary_s3 = primary_s3 or FakeS3()
    result = run_document_upload(
        repository=repository,
        state_db=FakeStateDb(),
        yadisk=yadisk,
        primary_s3=primary_s3,
        settings=settings(tmp_path),
        run_id=7,
        should_stop=lambda: False,
    )
    return result, repository, yadisk, primary_s3


def test_upload_reuses_verified_cache_without_yandex_traversal(tmp_path: Path) -> None:
    content = b"cached-document"
    row = document(content)
    (tmp_path / f"{row['md5']}.pdf").write_bytes(content)
    yadisk = FakeYaDisk({})

    result, repository, _, primary_s3 = run_one(tmp_path, row, yadisk=yadisk)

    assert result["uploaded"] == 1
    assert result["source_cache"] == 1
    assert yadisk.downloaded == []
    assert yadisk.listdir_calls == 0
    assert primary_s3.uploads == [("public-docs", f"{row['md5']}.pdf")]
    assert repository.saved[0][1]["document_url"].startswith(
        "https://s3.primary.example.test/public-docs/"
    )


def test_upload_downloads_cache_miss_from_persisted_yandex_path(tmp_path: Path) -> None:
    content = b"from-yandex"
    row = document(content, path="/documents/nested/book.pdf")
    yadisk = FakeYaDisk({row["ya_path"]: content})

    result, _, _, _ = run_one(tmp_path, row, yadisk=yadisk)

    assert result["source_yandex"] == 1
    assert yadisk.downloaded == [row["ya_path"]]
    assert yadisk.listdir_calls == 0
    assert (tmp_path / f"{row['md5']}.pdf").read_bytes() == content


def test_upload_preserves_whitespace_in_persisted_yandex_path(tmp_path: Path) -> None:
    content = b"space-path"
    path = "/documents/ascii-space /book .pdf"
    row = document(content, path=path)
    yadisk = FakeYaDisk({path: content})

    result, _, _, _ = run_one(tmp_path, row, yadisk=yadisk)

    assert result["source_yandex"] == 1
    assert yadisk.downloaded == [path]


def test_unavailable_yandex_document_is_skipped_and_left_pending(tmp_path: Path) -> None:
    row = document(b"missing", path="/documents/missing.pdf")
    result, repository, _, primary_s3 = run_one(
        tmp_path, row, yadisk=FakeYaDisk({})
    )

    assert result["skipped_download"] == 1
    assert result["failed"] == 0
    assert result["pending_after"] == 1
    assert repository.saved == []
    assert primary_s3.uploads == []
    assert _result_exit_code(result) == 0


def test_existing_verified_object_is_checkpointed_without_reupload(tmp_path: Path) -> None:
    content = b"already-uploaded"
    row = document(content)
    (tmp_path / f"{row['md5']}.pdf").write_bytes(content)
    primary_s3 = FakeS3()
    primary_s3.objects[("public-docs", f"{row['md5']}.pdf")] = {
        "ContentLength": len(content),
        "ETag": row["md5"],
        "Metadata": {},
        "Body": content,
    }

    result, repository, _, _ = run_one(
        tmp_path, row, yadisk=FakeYaDisk({}), primary_s3=primary_s3
    )

    assert result["recovered_existing"] == 1
    assert result["uploaded"] == 0
    assert primary_s3.uploads == []
    assert repository.saved


def test_invalid_existing_object_is_overwritten_and_partial_upload_is_aborted(
    tmp_path: Path,
) -> None:
    content = b"replacement"
    row = document(content)
    key = f"{row['md5']}.pdf"
    (tmp_path / key).write_bytes(content)
    primary_s3 = FakeS3()
    primary_s3.objects[("public-docs", key)] = {
        "ContentLength": 1,
        "ETag": "wrong",
        "Metadata": {},
        "Body": b"x",
    }
    primary_s3.incomplete_uploads[("public-docs", key)] = ["unfinished"]

    result, repository, _, _ = run_one(
        tmp_path, row, yadisk=FakeYaDisk({}), primary_s3=primary_s3
    )

    assert result["uploaded"] == 1
    assert result["reuploaded"] == 1
    assert primary_s3.uploads == [("public-docs", key)]
    assert primary_s3.aborted_uploads == [("public-docs", key, "unfinished")]
    assert repository.saved


def test_failed_post_upload_verification_leaves_row_pending(tmp_path: Path) -> None:
    content = b"unverified-upload"
    row = document(content)
    (tmp_path / f"{row['md5']}.pdf").write_bytes(content)

    result, repository, _, _ = run_one(
        tmp_path,
        row,
        yadisk=FakeYaDisk({}),
        primary_s3=FakeS3(omit_upload_metadata=True),
    )

    assert result["failed"] == 1
    assert result["pending_after"] == 1
    assert repository.saved == []


def test_new_upload_is_removed_when_sync_changes_row_before_checkpoint(
    tmp_path: Path,
) -> None:
    content = b"stale-upload"
    row = document(content)
    (tmp_path / f"{row['md5']}.pdf").write_bytes(content)
    repository = FakeRepository([row], reject_checkpoint=True)
    primary_s3 = FakeS3()

    result = run_document_upload(
        repository=repository,
        state_db=FakeStateDb(),
        yadisk=FakeYaDisk({}),
        primary_s3=primary_s3,
        settings=settings(tmp_path),
        run_id=9,
        should_stop=lambda: False,
    )

    key = f"{row['md5']}.pdf"
    assert result["checkpoint_raced"] == 1
    assert result["stale_upload_cleaned"] == 1
    assert primary_s3.uploads == [("public-docs", key)]
    assert primary_s3.deletes == [("public-docs", key)]
    assert ("public-docs", key) not in primary_s3.objects
    assert repository.saved == []


def test_verified_existing_object_is_not_removed_after_checkpoint_race(
    tmp_path: Path,
) -> None:
    content = b"existing-stale"
    row = document(content)
    key = f"{row['md5']}.pdf"
    (tmp_path / key).write_bytes(content)
    repository = FakeRepository([row], reject_checkpoint=True)
    primary_s3 = FakeS3()
    primary_s3.objects[("public-docs", key)] = {
        "ContentLength": len(content),
        "ETag": row["md5"],
        "Metadata": {},
        "Body": content,
    }

    result = run_document_upload(
        repository=repository,
        state_db=FakeStateDb(),
        yadisk=FakeYaDisk({}),
        primary_s3=primary_s3,
        settings=settings(tmp_path),
        run_id=10,
        should_stop=lambda: False,
    )

    assert result["checkpoint_raced"] == 1
    assert result["stale_upload_cleaned"] == 0
    assert primary_s3.deletes == []
    assert ("public-docs", key) in primary_s3.objects


def test_restricted_upload_uses_private_bucket_and_removes_public_copy(
    tmp_path: Path,
) -> None:
    content = b"private-document"
    row = document(content, path="/documents/private/book.pdf", restricted=True)
    (tmp_path / f"{row['md5']}.pdf").write_bytes(content)
    primary_s3 = FakeS3()
    primary_s3.objects[("public-docs", f"{row['md5']}.pdf")] = {
        "ContentLength": len(content),
        "ETag": row["md5"],
        "Metadata": {},
        "Body": content,
    }

    result, repository, _, _ = run_one(
        tmp_path, row, yadisk=FakeYaDisk({}), primary_s3=primary_s3
    )

    assert result["private_cleaned"] == 1
    assert primary_s3.uploads == [("private-docs", f"{row['md5']}.pdf")]
    assert primary_s3.deletes == [("public-docs", f"{row['md5']}.pdf")]
    assert str(repository.saved[0][1]["document_url"]).startswith("enc:")


def test_graceful_stop_finishes_current_document(tmp_path: Path) -> None:
    first = document(b"first", path="/documents/first.pdf")
    second = document(b"second", path="/documents/second.pdf")
    repository = FakeRepository([first, second])
    primary_s3 = FakeS3()
    result = run_document_upload(
        repository=repository,
        state_db=FakeStateDb(),
        yadisk=FakeYaDisk(
            {first["ya_path"]: b"first", second["ya_path"]: b"second"}
        ),
        primary_s3=primary_s3,
        settings=settings(tmp_path),
        run_id=8,
        should_stop=lambda: len(primary_s3.uploads) >= 1,
    )

    assert result["stopped"] is True
    assert result["processed"] == 1
    assert result["pending_after"] == 1


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
