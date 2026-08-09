from __future__ import annotations

import base64
from pathlib import Path

from app.document_storage import DocumentStorageSettings, S3ConnectionSettings
from app.modules.library.document_access import resolve_stored_document_url
from app.modules.runtime_shared_utils import encrypt


def _settings() -> DocumentStorageSettings:
    return DocumentStorageSettings(
        cache_path=Path("/tmp/manzara-document-access-test"),
        source_path="/documents",
        restricted_path="/documents/private",
        filtered_out_path="/documents/filtered-out",
        primary=S3ConnectionSettings(
            endpoint_url="https://s3.primary.test",
            region_name="eu-test-1",
            access_key_id="primary-key",
            secret_access_key="primary-secret",
        ),
        legacy=S3ConnectionSettings(
            endpoint_url="https://storage.yandex.test",
            region_name="ru-test-1",
            access_key_id="legacy-key",
            secret_access_key="legacy-secret",
        ),
        public_bucket="public-docs",
        private_bucket="private-docs",
        legacy_public_bucket="legacy-public",
        legacy_private_bucket="legacy-private",
        upstream_bucket="upstream",
        encryption_key=base64.urlsafe_b64encode(b"0" * 32).decode(),
    )


def test_document_access_prefers_verified_primary_public_url() -> None:
    result = resolve_stored_document_url(
        {
            "document_url": "https://s3.primary.test/public-docs/book.pdf",
            "ya_public_url": "https://disk.yandex.test/book",
            "primary_storage_verified_at": "2026-08-05T12:00:00+00:00",
        },
        settings=_settings(),
        client_factory=lambda _connection: None,
    )

    assert result == "https://s3.primary.test/public-docs/book.pdf"


def test_document_access_signs_private_primary_url() -> None:
    settings = _settings()
    encrypted = encrypt(
        "https://s3.primary.test/private-docs/book.pdf",
        {"encryption_key": settings.encryption_key},
    )

    class FakeS3:
        def generate_presigned_url(self, operation, *, Params, ExpiresIn):  # noqa: ANN001, N803
            assert operation == "get_object"
            assert Params == {"Bucket": "private-docs", "Key": "book.pdf"}
            assert ExpiresIn == 900
            return "https://signed.primary.test/book.pdf"

    result = resolve_stored_document_url(
        {"document_url": encrypted},
        settings=settings,
        client_factory=lambda connection: FakeS3()
        if connection is settings.primary
        else None,
    )

    assert result == "https://signed.primary.test/book.pdf"


def test_document_access_signs_legacy_private_url() -> None:
    settings = _settings()
    encrypted = encrypt(
        "https://storage.yandex.test/legacy-private/book.pdf",
        {"encryption_key": settings.encryption_key},
    )

    class FakeS3:
        def generate_presigned_url(self, operation, *, Params, ExpiresIn):  # noqa: ANN001, N803
            assert operation == "get_object"
            assert Params == {"Bucket": "legacy-private", "Key": "book.pdf"}
            return "https://signed.yandex.test/book.pdf"

    result = resolve_stored_document_url(
        {"document_url": encrypted},
        settings=settings,
        client_factory=lambda connection: FakeS3()
        if connection is settings.legacy
        else None,
    )

    assert result == "https://signed.yandex.test/book.pdf"
