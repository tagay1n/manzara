from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
import zipfile

from app.document_storage import DocumentStorageSettings, S3ConnectionSettings
from app.modules.maintenance.content_storage_migration import (
    ContentMigrationCandidate,
    rewrite_content_archive,
    run_content_storage_migration,
)
from app.modules.maintenance.tasks import (
    MAINTENANCE_MIGRATE_PDF_CONTENT_TASK_ID,
    maintenance_task_definitions,
)
from app.modules.maintenance.config import MaintenanceSettings


def _archive(md5: str, markdown: str) -> bytes:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as target:
        target.writestr(f"{md5}.md", markdown)
    return stream.getvalue()


def test_archive_rewrite_preserves_member_and_only_rewrites_legacy_images() -> None:
    md5 = "a" * 32
    source = _archive(
        md5,
        f'<img src="https://storage.yandexcloud.net/ttimg/{md5}-2-0.png">\n'
        "![external](https://example.test/image.png)\n",
    )

    rewritten, keys, member = rewrite_content_archive(
        source,
        md5=md5,
        legacy_endpoint="https://storage.yandexcloud.net",
        legacy_bucket="ttimg",
        primary_endpoint="https://s3.eu-central-003.backblazeb2.com",
        primary_bucket="ttimgs",
    )

    assert keys == (f"{md5}-2-0.png",)
    assert member == f"{md5}.md"
    with zipfile.ZipFile(BytesIO(rewritten)) as archive:
        markdown = archive.read(member).decode()
    assert f"https://s3.eu-central-003.backblazeb2.com/ttimgs/{md5}-2-0.png" in markdown
    assert "https://example.test/image.png" in markdown
    assert "storage.yandexcloud.net/ttimg" not in markdown


def test_archive_rewrite_ignores_markdown_delimiters_around_external_urls() -> None:
    md5 = "a" * 32
    image_key = f"{md5}-2-0.png"
    external_link = "[http://plr.iling-ran.ru](http://plr.iling-ran.ru)"
    source = _archive(
        md5,
        f"{external_link}\n"
        f'<img src="https://storage.yandexcloud.net/ttimg/{image_key}">\n',
    )

    rewritten, keys, member = rewrite_content_archive(
        source,
        md5=md5,
        legacy_endpoint="https://storage.yandexcloud.net",
        legacy_bucket="ttimg",
        primary_endpoint="https://s3.eu-central-003.backblazeb2.com",
        primary_bucket="ttimgs",
    )

    assert keys == (image_key,)
    with zipfile.ZipFile(BytesIO(rewritten)) as archive:
        markdown = archive.read(member).decode()
    assert external_link in markdown
    assert (
        f"https://s3.eu-central-003.backblazeb2.com/ttimgs/{image_key}"
        in markdown
    )


def test_archive_rewrite_rejects_cross_document_image_key() -> None:
    md5 = "a" * 32
    source = _archive(
        md5,
        '![bad](https://storage.yandexcloud.net/ttimg/' + "b" * 32 + "-1-0.png)",
    )

    try:
        rewrite_content_archive(
            source,
            md5=md5,
            legacy_endpoint="https://storage.yandexcloud.net",
            legacy_bucket="ttimg",
            primary_endpoint="https://s3.example.test",
            primary_bucket="ttimgs",
        )
    except ValueError as exc:
        assert "does not belong" in str(exc)
    else:
        raise AssertionError("cross-document image must be rejected")


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict] = {}
        self.uploads: list[tuple[str, str]] = []
        self.deletes: list[tuple[str, str]] = []
        self.transfer_thread_settings: list[bool] = []

    def add(self, bucket: str, key: str, body: bytes) -> None:
        self.objects[(bucket, key)] = {
            "Body": body,
            "ContentLength": len(body),
            "ETag": hashlib.md5(body).hexdigest(),  # noqa: S324
            "Metadata": {},
        }

    def head_object(self, *, Bucket, Key):  # noqa: N803, ANN001
        item = self.objects.get((Bucket, Key))
        if item is None:
            raise KeyError((Bucket, Key))
        return {key: value for key, value in item.items() if key != "Body"}

    def download_file(self, bucket, key, filename, Config):  # noqa: N803, ANN001
        self.transfer_thread_settings.append(bool(Config.use_threads))
        Path(filename).write_bytes(self.objects[(bucket, key)]["Body"])

    def upload_file(  # noqa: N803, ANN001
        self, filename, bucket, key, ExtraArgs, Config
    ):
        self.transfer_thread_settings.append(bool(Config.use_threads))
        body = Path(filename).read_bytes()
        self.objects[(bucket, key)] = {
            "Body": body,
            "ContentLength": len(body),
            "ETag": hashlib.md5(body).hexdigest(),  # noqa: S324
            "Metadata": dict(ExtraArgs["Metadata"]),
        }
        self.uploads.append((bucket, key))

    def delete_object(self, *, Bucket, Key):  # noqa: N803, ANN001
        self.objects.pop((Bucket, Key), None)
        self.deletes.append((Bucket, Key))


class FakeRepository:
    def __init__(self, candidate: ContentMigrationCandidate) -> None:
        self.candidate = candidate
        self.cutovers: list[tuple[str, str, str]] = []
        self.completed: list[str] = []
        self.failed: list[str] = []
        self.images: dict[str, dict] = {}
        self.archive_deleted = False
        self.archive: dict = {}

    def list_work(self, **_kwargs):
        return [] if self.completed else [self.candidate]

    def count_pending(self):
        return 0 if self.completed else 1

    def start(self, *_args, **_kwargs):
        return None

    def checkpoint_archive(self, *_args, **_kwargs):
        self.archive = {
            "source_archive_etag": _kwargs["source_etag"],
            "source_archive_size": _kwargs["source_size"],
            "destination_archive_etag": _kwargs["destination_etag"],
            "destination_archive_size": _kwargs["destination_size"],
            "destination_archive_sha256": _kwargs["sha256"],
        }

    def get_archive_checkpoint(self, _md5):  # noqa: ANN001
        return dict(self.archive)

    def checkpoint_image(self, _md5, key, **values):  # noqa: ANN001
        self.images[key] = dict(values)

    def retain_images(self, _md5, image_keys):  # noqa: ANN001
        expected = set(image_keys)
        self.images = {
            key: value for key, value in self.images.items() if key in expected
        }

    def list_images(self, _md5):  # noqa: ANN001
        return [{"image_key": key, **value} for key, value in self.images.items()]

    def cutover(self, md5, *, expected_url, destination_url, **_kwargs):  # noqa: ANN001
        self.cutovers.append((md5, expected_url, destination_url))
        return True

    def checkpoint_archive_deleted(self, *_args, **_kwargs):
        self.archive_deleted = True

    def checkpoint_image_deleted(self, _md5, key):  # noqa: ANN001
        self.images[key]["source_deleted"] = True

    def complete(self, md5, **_kwargs):  # noqa: ANN001
        self.completed.append(md5)

    def fail(self, md5, **_kwargs):  # noqa: ANN001
        self.failed.append(md5)


class FakeStateDb:
    def update_run_progress(self, *_args, **_kwargs):
        return None

    def insert_event(self, *_args, **_kwargs):
        return None


def _settings(tmp_path: Path) -> DocumentStorageSettings:
    return DocumentStorageSettings(
        cache_path=tmp_path / "cache",
        source_path="/docs",
        restricted_path="/private",
        filtered_out_path="/filtered",
        primary=S3ConnectionSettings("https://b2.test", "b2", "key", "secret"),
        legacy=S3ConnectionSettings(
            "https://storage.yandexcloud.net", "yc", "key", "secret"
        ),
        public_bucket="docs",
        private_bucket="private",
        legacy_public_bucket="legacy-docs",
        legacy_private_bucket="legacy-private",
        encryption_key="unused",
        content_bucket="ttcontent",
        content_images_bucket="ttimgs",
        legacy_content_bucket="ttcontent",
        legacy_content_images_bucket="ttimg",
    )


def test_single_worker_migrates_cutovers_and_deletes_in_order(tmp_path: Path) -> None:
    md5 = "a" * 32
    image_key = f"{md5}-2-0.png"
    source_url = f"https://storage.yandexcloud.net/ttcontent/{md5}.zip"
    candidate = ContentMigrationCandidate(md5, "application/pdf", source_url, "pending")
    repository = FakeRepository(candidate)
    legacy = FakeS3()
    primary = FakeS3()
    legacy.add("ttimg", image_key, b"png-bytes")
    legacy.add(
        "ttcontent",
        f"{md5}.zip",
        _archive(
            md5,
            f'<img src="https://storage.yandexcloud.net/ttimg/{image_key}">',
        ),
    )

    result = run_content_storage_migration(
        repository=repository,
        state_db=FakeStateDb(),
        legacy_s3=legacy,
        primary_s3=primary,
        settings=_settings(tmp_path),
        workspace=tmp_path / "run",
        run_id=9,
        should_stop=lambda: False,
        public_check=lambda _url: True,
    )

    assert result["migrated"] == 1
    assert repository.cutovers == [
        (md5, source_url, f"https://b2.test/ttcontent/{md5}.zip")
    ]
    assert primary.uploads == [("ttimgs", image_key), ("ttcontent", f"{md5}.zip")]
    assert legacy.deletes == [("ttcontent", f"{md5}.zip"), ("ttimg", image_key)]
    assert repository.completed == [md5]
    assert primary.transfer_thread_settings == [False, False]
    assert legacy.transfer_thread_settings == [False, False]


def test_stop_before_first_document_makes_no_remote_changes(tmp_path: Path) -> None:
    md5 = "a" * 32
    candidate = ContentMigrationCandidate(
        md5,
        "application/pdf",
        f"https://storage.yandexcloud.net/ttcontent/{md5}.zip",
        "pending",
    )
    repository = FakeRepository(candidate)
    legacy = FakeS3()
    primary = FakeS3()

    result = run_content_storage_migration(
        repository=repository,
        state_db=FakeStateDb(),
        legacy_s3=legacy,
        primary_s3=primary,
        settings=_settings(tmp_path),
        workspace=tmp_path / "run",
        run_id=9,
        should_stop=lambda: True,
        public_check=lambda _url: True,
    )

    assert result["stopped"] is True
    assert primary.uploads == []
    assert legacy.deletes == []


def test_task_is_registered_in_library_catalog_without_worker_option(tmp_path: Path) -> None:
    settings = MaintenanceSettings(
        monocorpus_repo_path=tmp_path, pgbackrest_stanza="monocorpus"
    )
    task = next(
        item
        for item in maintenance_task_definitions(settings)
        if item["task_id"] == MAINTENANCE_MIGRATE_PDF_CONTENT_TASK_ID
    )

    assert task["panel_id"] == "library"
    assert "migrate_pdf_content" in task["command"]["value"]
    assert "worker" not in task["command"]["value"]
