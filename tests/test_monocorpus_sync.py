"""Guarded monocorpus synchronization behavior tests."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from botocore.exceptions import ClientError

from app.document_storage import DocumentStorageSettings, S3ConnectionSettings
from app.modules.maintenance.runtime.sync_monocorpus import (
    _apply_cleanup,
    _cleanup_managed_storage,
    _delete_prefix,
    _publish_progress,
    run_monocorpus_sync,
)


class _YaDisk:
    def __init__(self, files: dict[str, bytes], *, public_metadata: bool = True) -> None:
        self.files = dict(files)
        self.public_metadata = public_metadata
        self.published: list[str] = []
        self.timeline: list[tuple[str, str]] = []

    def listdir(self, path, **_kwargs):  # noqa: ANN001
        prefix = str(path).rstrip("/") + "/"
        direct: dict[str, dict] = {}
        for remote_path, content in self.files.items():
            if not remote_path.startswith(prefix):
                continue
            remainder = remote_path[len(prefix) :]
            first, _, tail = remainder.partition("/")
            if tail:
                direct[first] = {
                    "name": first,
                    "path": prefix + first,
                    "type": "dir",
                }
                continue
            direct[first] = {
                "name": first,
                "path": remote_path,
                "type": "file",
                "size": len(content),
                "md5": hashlib.md5(content).hexdigest(),  # noqa: S324
                "mime_type": (
                    "application/zip"
                    if remote_path.lower().endswith(".zip")
                    else "application/pdf"
                ),
                "resource_id": "resource:" + first,
                "public_key": "key:" + first if self.public_metadata else "",
                "public_url": "https://disk/" + first if self.public_metadata else "",
            }
        return iter(direct.values())

    def get_meta(self, path, **_kwargs):  # noqa: ANN001
        if str(path) not in self.files:
            from yadisk.exceptions import PathNotFoundError

            raise PathNotFoundError(error_type="DiskNotFoundError", msg="missing")
        name = Path(str(path)).name
        content = self.files[str(path)]
        return {
            "md5": hashlib.md5(content).hexdigest(),  # noqa: S324
            "resource_id": "resource:" + name,
            "public_key": "published:" + name if str(path) in self.published else "",
            "public_url": "https://disk/published/" + name if str(path) in self.published else "",
        }

    def publish(self, path):  # noqa: ANN001
        self.published.append(str(path))

    def remove(self, path, permanently=False):  # noqa: ANN001
        assert permanently is True
        self.timeline.append(("remove", str(path)))
        self.files.pop(str(path), None)


class _Repository:
    def __init__(self, yadisk: _YaDisk, documents=None) -> None:  # noqa: ANN001
        self.yadisk = yadisk
        self.documents = dict(documents or {})
        self.timeline: list[tuple[str, str]] = []
        self.saved: list[dict] = []
        self._cleanup_id = 0

    def list_active_cleanup(self):
        return []

    def list_documents(self):
        return {key: dict(value) for key, value in self.documents.items()}

    def enqueue_cleanup(self, payload):  # noqa: ANN001
        self._cleanup_id += 1
        self.timeline.append(("enqueue", str(payload["source_path"])))
        self.yadisk.timeline.append(("enqueue", str(payload["source_path"])))
        return self._cleanup_id, True

    def mark_cleanup_running(self, *_args, **_kwargs):
        return None

    def mark_cleanup_completed(self, *_args, **_kwargs):
        return None

    def mark_cleanup_failed(self, *_args, **_kwargs):
        return None

    def mark_cleanup_canceled(self, cleanup_id, reason):  # noqa: ANN001
        self.timeline.append(("canceled", f"{cleanup_id}:{reason}"))

    def save_discovered_document(self, payload):  # noqa: ANN001
        item = dict(payload)
        created = item["md5"] not in self.documents
        self.documents[item["md5"]] = item
        self.saved.append(item)
        return created


class _Db:
    def __init__(self) -> None:
        self.progress: list[dict] = []

    def update_run_progress(self, *_args, **_kwargs):
        self.progress.append(dict(_args[1]))
        return None

    def insert_event(self, *_args, **_kwargs):
        return None


def test_cleanup_progress_exposes_queue_position_and_total() -> None:
    db = _Db()

    _publish_progress(
        db,
        12,
        {"cleanups_completed": 4},
        "/documents/book.pdf",
        stage="cleanup",
        current=4,
        total=10,
    )

    assert db.progress == [
        {
            "stage": "cleanup",
            "current_path": "/documents/book.pdf",
            "cleanups_completed": 4,
            "current": 4,
            "total": 10,
        }
    ]


def test_missing_cleanup_source_and_target_is_canceled_without_deleting_state() -> None:
    yadisk = _YaDisk({})
    repository = _Repository(yadisk)

    removed, outcome = _apply_cleanup(
        {
            "cleanup_id": 146,
            "scope": "document",
            "action": "move",
            "reason": "non_document",
            "md5": "a" * 32,
            "source_path": "/documents/missing.ttf",
            "target_path": "/filtered/missing.ttf",
            "status": "failed",
        },
        repository=repository,
        yadisk=yadisk,
        primary_s3=_S3(),
        legacy_s3=_S3(),
        settings=_settings(),
        config={"yandex": {"cloud": {"bucket": {}}}},
        run_id=630,
        missing_legacy_buckets=set(),
    )

    assert removed == 0
    assert outcome == "canceled"
    assert repository.timeline == [
        ("canceled", "146:Cleanup source and verified target are both missing")
    ]


class _S3:
    def list_objects_v2(self, **_kwargs):
        return {"Contents": [], "IsTruncated": False}


class _MissingBucketS3(_S3):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_objects_v2(self, *, Bucket, **_kwargs):  # noqa: N803, ANN001
        self.calls.append(Bucket)
        if Bucket == "missing-legacy":
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchBucket", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "ListObjectsV2",
            )
        return {"Contents": [], "IsTruncated": False}


def _settings() -> DocumentStorageSettings:
    connection = S3ConnectionSettings("https://s3.test", "region", "key", "secret")
    return DocumentStorageSettings(
        cache_path=Path("/tmp/cache"),
        source_path="/documents",
        restricted_path="/documents/private",
        filtered_out_path="/filtered",
        primary=connection,
        legacy=connection,
        public_bucket="public",
        private_bucket="private",
        legacy_public_bucket="legacy-public",
        legacy_private_bucket="legacy-private",
        upstream_bucket="upstream",
        encryption_key=base64.urlsafe_b64encode(b"0" * 32).decode(),
    )


def test_duplicate_md5_is_queued_before_yandex_removal() -> None:
    content = b"same"
    yadisk = _YaDisk(
        {"/documents/a.pdf": content, "/documents/b.pdf": content}
    )
    repository = _Repository(yadisk)

    result = run_monocorpus_sync(
        repository=repository,
        db=_Db(),
        yadisk=yadisk,
        primary_s3=_S3(),
        legacy_s3=_S3(),
        settings=_settings(),
        config={"yandex": {"cloud": {"bucket": {}}}},
        run_id=3,
        should_stop=lambda: False,
    )

    assert result["duplicate_resources_queued"] == 1
    assert [event[0] for event in yadisk.timeline] == ["enqueue", "remove"]


def test_restricted_and_existing_public_documents_are_not_published() -> None:
    restricted = b"restricted"
    public = b"public"
    restricted_md5 = hashlib.md5(restricted).hexdigest()  # noqa: S324
    public_md5 = hashlib.md5(public).hexdigest()  # noqa: S324
    yadisk = _YaDisk(
        {
            "/documents/private/secret.pdf": restricted,
            "/documents/public.pdf": public,
        },
        public_metadata=False,
    )
    repository = _Repository(
        yadisk,
        documents={
            public_md5: {
                "md5": public_md5,
                "mime_type": "application/pdf",
                "ya_path": "/documents/public.pdf",
                "ya_public_url": "https://disk/existing",
                "ya_public_key": "existing-key",
                "ya_resource_id": "resource:public.pdf",
                "full": True,
                "sharing_restricted": False,
            }
        },
    )

    result = run_monocorpus_sync(
        repository=repository,
        db=_Db(),
        yadisk=yadisk,
        primary_s3=_S3(),
        legacy_s3=_S3(),
        settings=_settings(),
        config={"yandex": {"cloud": {"bucket": {}}}},
        run_id=4,
        should_stop=lambda: False,
    )

    assert result["failed"] == 0
    assert yadisk.published == []
    assert repository.documents[restricted_md5]["ya_public_url"] is None
    assert repository.documents[public_md5]["ya_public_url"] == "https://disk/existing"


def test_filtered_resource_is_not_published_or_saved() -> None:
    yadisk = _YaDisk({"/documents/archive.zip": b"not a document"})
    repository = _Repository(yadisk)

    result = run_monocorpus_sync(
        repository=repository,
        db=_Db(),
        yadisk=yadisk,
        primary_s3=_S3(),
        legacy_s3=_S3(),
        settings=_settings(),
        config={"yandex": {"cloud": {"bucket": {}}}},
        run_id=5,
        should_stop=lambda: False,
    )

    assert result["filtered"] == 1
    assert repository.saved == []
    assert yadisk.published == []


def test_cleanup_treats_absent_legacy_bucket_as_empty() -> None:
    legacy_s3 = _MissingBucketS3()
    missing_buckets: set[str] = set()
    removed = _cleanup_managed_storage(
        md5="a" * 32,
        primary_s3=_S3(),
        legacy_s3=legacy_s3,
        settings=_settings(),
        config={"yandex": {"cloud": {"bucket": {"obsolete": "missing-legacy"}}}},
        missing_legacy_buckets=missing_buckets,
    )
    removed += _cleanup_managed_storage(
        md5="b" * 32,
        primary_s3=_S3(),
        legacy_s3=legacy_s3,
        settings=_settings(),
        config={"yandex": {"cloud": {"bucket": {"obsolete": "missing-legacy"}}}},
        missing_legacy_buckets=missing_buckets,
    )

    assert removed == 0
    assert legacy_s3.calls.count("missing-legacy") == 1


class _MutablePagedS3:
    def __init__(self) -> None:
        self.keys = ["abc-1", "abc-2", "abc-3"]
        self.requests: list[dict] = []

    def list_objects_v2(self, **request):  # noqa: ANN003
        self.requests.append(dict(request))
        assert "ContinuationToken" not in request
        matches = [key for key in self.keys if key.startswith(request["Prefix"])]
        page = matches[:2]
        return {
            "Contents": [{"Key": key} for key in page],
            "IsTruncated": len(matches) > len(page),
        }

    def delete_object(self, *, Bucket, Key):  # noqa: N803, ANN001
        _ = Bucket
        self.keys.remove(Key)


def test_cleanup_restarts_mutated_s3_listing_from_first_page_until_empty() -> None:
    s3 = _MutablePagedS3()

    removed = _delete_prefix(s3, "documents", "abc")

    assert removed == 3
    assert s3.keys == []
