"""Tests for shared primary document-storage primitives."""

from __future__ import annotations

import hashlib
import base64
from pathlib import Path

import pytest

from app.document_storage import (
    DocumentStorageSettings,
    S3ConnectionSettings,
    calculate_multipart_etag,
    download_verified_primary_document,
    document_object_key,
    find_valid_cache_file,
    load_document_storage_settings,
    materialize_cached_document,
    resolve_document_download_url,
)
from app.modules.runtime_shared_utils import encrypt


def test_load_document_storage_settings_uses_explicit_sources_and_buckets(
    tmp_path: Path,
) -> None:
    payload = {
        "documents": {
            "cache_path": str(tmp_path / "cache"),
            "primary_storage": {
                "endpoint_url": "https://s3.eu-central-003.backblazeb2.com",
                "region_name": "eu-central-003",
                "access_key_id": "b2-key-id",
                "secret_access_key": "b2-app-key",
                "bucket": {
                    "public": "manzara-documents",
                    "private": "manzara-documents-private",
                    "content": "ttcontent",
                    "content_images": "ttcontent-images",
                },
            },
        },
        "yandex": {
            "disk": {
                "oauth_token": "disk-token",
                "documents": {
                    "source_path": "/documents",
                    "restricted_path": "/documents/private",
                    "filtered_out_path": "/documents/filtered-out",
                },
            },
            "cloud": {
                "aws_access_key_id": "access",
                "aws_secret_access_key": "secret",
                "bucket": {
                    "document": "public-docs",
                    "document_private": "private-docs",
                    "upstream_metadata": "upstream",
                },
            },
        },
        "encryption_key": "key",
    }

    settings = load_document_storage_settings(payload)

    assert settings.cache_path == tmp_path / "cache"
    assert settings.source_path == "/documents"
    assert settings.restricted_path == "/documents/private"
    assert settings.filtered_out_path == "/documents/filtered-out"
    assert settings.primary.endpoint_url == "https://s3.eu-central-003.backblazeb2.com"
    assert settings.primary.region_name == "eu-central-003"
    assert settings.primary.access_key_id == "b2-key-id"
    assert settings.public_bucket == "manzara-documents"
    assert settings.private_bucket == "manzara-documents-private"
    assert settings.content_bucket == "ttcontent"
    assert settings.content_images_bucket == "ttcontent-images"
    assert settings.legacy.endpoint_url == "https://storage.yandexcloud.net"
    assert settings.legacy_public_bucket == "public-docs"
    assert not hasattr(settings, "upstream_bucket")

    del payload["documents"]["primary_storage"]["bucket"]["private"]
    with pytest.raises(RuntimeError, match="documents.primary_storage.bucket.private"):
        load_document_storage_settings(payload)


def test_cache_candidate_must_match_md5_and_ignores_partial_files(tmp_path: Path) -> None:
    content = b"cached-document"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    (tmp_path / f"{digest}.crdownload").write_bytes(content)
    (tmp_path / f"{digest}.pdf").write_bytes(b"wrong")
    assert find_valid_cache_file(tmp_path, digest) is None

    valid = tmp_path / f"{digest}.docx"
    valid.write_bytes(content)
    assert find_valid_cache_file(tmp_path, digest) == valid


def test_materialize_cached_document_reuses_verified_shared_file(
    tmp_path: Path,
) -> None:
    content = b"shared-document"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    cached = tmp_path / f"{digest}.pdf"
    cached.write_bytes(content)
    downloads: list[Path] = []

    result = materialize_cached_document(
        cache_path=tmp_path,
        expected_md5=digest,
        extension=".pdf",
        download=lambda target: downloads.append(target),
    )

    assert result == cached
    assert downloads == []


def test_materialize_cached_document_replaces_invalid_file_atomically(
    tmp_path: Path,
) -> None:
    content = b"downloaded-document"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    cached = tmp_path / f"{digest}.pdf"
    cached.write_bytes(b"corrupt")

    result = materialize_cached_document(
        cache_path=tmp_path,
        expected_md5=digest,
        extension="pdf",
        download=lambda target: target.write_bytes(content),
    )

    assert result == cached
    assert cached.read_bytes() == content
    assert list(tmp_path.glob("*.download")) == []


def test_multipart_etag_matches_boto3_default_chunks(tmp_path: Path) -> None:
    content = b"a" * (8 * 1024 * 1024) + b"tail"
    source = tmp_path / "source.bin"
    source.write_bytes(content)
    first = hashlib.md5(content[: 8 * 1024 * 1024]).digest()  # noqa: S324
    second = hashlib.md5(b"tail").digest()  # noqa: S324
    expected = f"{hashlib.md5(first + second).hexdigest()}-2"  # noqa: S324

    assert calculate_multipart_etag(source) == expected


@pytest.mark.parametrize(
    ("path", "mime_type", "expected"),
    [
        ("/source/Book.PDF", "application/pdf", "a" * 32 + ".pdf"),
        ("/source/no-extension", "application/pdf", "a" * 32 + ".pdf"),
        ("/source/archive.weird!", "application/octet-stream", "a" * 32 + ".bin"),
    ],
)
def test_document_object_key_is_flat_and_normalized(
    path: str,
    mime_type: str,
    expected: str,
) -> None:
    assert document_object_key("a" * 32, path, mime_type) == expected


def test_private_document_url_is_resolved_to_short_lived_signed_url() -> None:
    encryption_key = base64.urlsafe_b64encode(b"0" * 32).decode()
    private_url = "https://s3.example.test/private-docs/book.pdf"
    encrypted = encrypt(private_url, {"encryption_key": encryption_key})

    class FakeS3:
        def generate_presigned_url(self, operation, *, Params, ExpiresIn):  # noqa: N803, ANN001
            assert operation == "get_object"
            assert Params == {"Bucket": "private-docs", "Key": "book.pdf"}
            assert ExpiresIn == 900
            return "https://signed.example/book.pdf"

    assert resolve_document_download_url(
        document_url=encrypted,
        fallback_url=None,
        encryption_key=encryption_key,
        endpoint_url="https://s3.example.test",
        private_bucket="private-docs",
        s3=FakeS3(),
    ) == "https://signed.example/book.pdf"


def _primary_settings(tmp_path: Path) -> DocumentStorageSettings:
    connection = S3ConnectionSettings(
        endpoint_url="https://s3.example.test",
        region_name="eu-test",
        access_key_id="key",
        secret_access_key="secret",
    )
    return DocumentStorageSettings(
        cache_path=tmp_path,
        source_path="/unused",
        restricted_path="/unused/private",
        filtered_out_path="/unused/filtered",
        primary=connection,
        legacy=connection,
        public_bucket="public-docs",
        private_bucket="private-docs",
        legacy_public_bucket="unused",
        legacy_private_bucket="unused-private",
        encryption_key="unused",
    )


def test_primary_download_accepts_only_verified_backblaze_location(tmp_path: Path) -> None:
    content = b"primary-document"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324

    class FakeS3:
        def head_object(self, *, Bucket, Key):  # noqa: N803
            assert (Bucket, Key) == ("public-docs", f"{digest}.pdf")
            return {"ContentLength": len(content)}

        def download_file(self, bucket, key, target):  # noqa: ANN001
            assert (bucket, key) == ("public-docs", f"{digest}.pdf")
            Path(target).write_bytes(content)

    destination = tmp_path / "book.pdf"
    result = download_verified_primary_document(
        settings=_primary_settings(tmp_path),
        s3=FakeS3(),
        document_url=f"https://s3.example.test/public-docs/{digest}.pdf",
        expected_md5=digest,
        expected_size=len(content),
        destination=destination,
    )

    assert result == destination
    assert destination.read_bytes() == content


def test_primary_download_rejects_non_primary_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="primary Backblaze"):
        download_verified_primary_document(
            settings=_primary_settings(tmp_path),
            s3=object(),
            document_url="https://storage.yandexcloud.net/docs/book.pdf",
            expected_md5="a" * 32,
            destination=tmp_path / "book.pdf",
        )
