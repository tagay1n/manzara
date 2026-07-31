"""Tests for shared primary document-storage primitives."""

from __future__ import annotations

import hashlib
import base64
from pathlib import Path

import pytest

from app.document_storage import (
    calculate_multipart_etag,
    document_object_key,
    find_valid_cache_file,
    load_document_storage_settings,
    resolve_document_download_url,
)
from app.modules.runtime_shared_utils import encrypt


def test_load_document_storage_settings_uses_explicit_sources_and_buckets(
    tmp_path: Path,
) -> None:
    payload = {
        "documents": {"cache_path": str(tmp_path / "cache")},
        "yandex": {
            "disk": {
                "oauth_token": "disk-token",
                "documents": {
                    "source_path": "/documents",
                    "restricted_path": "/documents/private",
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
    assert settings.public_bucket == "public-docs"
    assert settings.private_bucket == "private-docs"

    del payload["yandex"]["cloud"]["bucket"]["document_private"]
    with pytest.raises(RuntimeError, match="document_private"):
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
