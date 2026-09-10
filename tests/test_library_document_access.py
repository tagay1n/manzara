from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import library_document_routes
from app.document_storage import DocumentStorageSettings, S3ConnectionSettings
from app.modules.library import document_access
from app.modules.library.document_access import (
    cache_document_for_local_open,
    resolve_local_cached_document,
    resolve_stored_document_url,
)
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


def test_cache_document_for_local_open_downloads_verified_primary_document(
    tmp_path: Path,
    monkeypatch,
) -> None:
    content = b"local review document"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    settings = _settings()
    settings = DocumentStorageSettings(**{**settings.__dict__, "cache_path": tmp_path})
    monkeypatch.setattr(
        document_access,
        "_load_document_row",
        lambda _state, _md5: {
            "md5": digest,
            "document_url": f"https://s3.primary.test/public-docs/{digest}.pdf",
            "primary_storage_size": len(content),
            "primary_storage_verified_at": "2026-09-10T12:00:00+00:00",
            "mime_type": "application/pdf",
            "ya_path": "/books/review.pdf",
        },
    )
    monkeypatch.setattr(document_access, "load_runtime_config", dict)
    monkeypatch.setattr(
        document_access, "load_document_storage_settings", lambda _payload: settings
    )

    class FakeS3:
        def head_object(self, *, Bucket, Key):  # noqa: ANN001, N803
            assert (Bucket, Key) == ("public-docs", f"{digest}.pdf")
            return {"ContentLength": len(content)}

        def download_file(self, bucket, key, target):  # noqa: ANN001
            assert (bucket, key) == ("public-docs", f"{digest}.pdf")
            Path(target).write_bytes(content)

    cached = cache_document_for_local_open(
        object(), digest, client_factory=lambda _connection: FakeS3()
    )

    assert cached.path == tmp_path / f"{digest}.pdf"
    assert cached.mime_type == "application/pdf"
    assert cached.path.read_bytes() == content


def test_cache_document_for_local_open_reuses_valid_cache_without_s3(
    tmp_path: Path,
    monkeypatch,
) -> None:
    content = b"already cached"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    (tmp_path / f"{digest}.pdf").write_bytes(content)
    settings = _settings()
    settings = DocumentStorageSettings(**{**settings.__dict__, "cache_path": tmp_path})
    monkeypatch.setattr(
        document_access,
        "_load_document_row",
        lambda _state, _md5: {
            "md5": digest,
            "document_url": f"https://s3.primary.test/public-docs/{digest}.pdf",
            "primary_storage_size": len(content),
            "primary_storage_verified_at": "2026-09-10T12:00:00+00:00",
            "mime_type": "application/pdf",
            "ya_path": "/books/review.pdf",
        },
    )
    monkeypatch.setattr(document_access, "load_runtime_config", dict)
    monkeypatch.setattr(
        document_access, "load_document_storage_settings", lambda _payload: settings
    )

    cached = cache_document_for_local_open(
        object(),
        digest,
        client_factory=lambda _connection: (_ for _ in ()).throw(
            AssertionError("cache hit must not create an S3 client")
        ),
    )

    assert cached.path == tmp_path / f"{digest}.pdf"


def test_resolve_local_cached_document_requires_catalog_record_and_valid_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    content = b"serve me"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    cached_path = tmp_path / f"{digest}.pdf"
    cached_path.write_bytes(content)
    settings = _settings()
    settings = DocumentStorageSettings(**{**settings.__dict__, "cache_path": tmp_path})
    monkeypatch.setattr(
        document_access,
        "_load_document_row",
        lambda _state, _md5: {
            "md5": digest,
            "mime_type": "application/pdf",
            "ya_path": "/books/original.pdf",
        },
    )
    monkeypatch.setattr(document_access, "load_runtime_config", dict)
    monkeypatch.setattr(
        document_access, "load_document_storage_settings", lambda _payload: settings
    )

    cached = resolve_local_cached_document(object(), digest)

    assert cached is not None
    assert cached.path == cached_path
    assert cached.source_name == "original.pdf"


def test_library_document_cache_and_local_open_routes(monkeypatch, tmp_path: Path) -> None:
    digest = "b" * 32
    local_path = tmp_path / f"{digest}.pdf"
    local_path.write_bytes(b"cached pdf")
    cached = document_access.CachedDocument(
        path=local_path,
        mime_type="application/pdf",
        source_name="book.pdf",
    )
    monkeypatch.setattr(
        library_document_routes,
        "cache_document_for_local_open",
        lambda _state, md5: cached if md5 == digest else None,
    )
    monkeypatch.setattr(
        library_document_routes,
        "resolve_local_cached_document",
        lambda _state, md5: cached if md5 == digest else None,
    )
    app = FastAPI()
    library_document_routes.register_library_document_routes(
        app,
        state_provider=lambda: SimpleNamespace(),
    )
    client = TestClient(app)

    cache_response = client.post(f"/api/library/documents/{digest}/cache")
    local_response = client.get(f"/api/library/documents/{digest}/local")

    assert cache_response.status_code == 200
    assert cache_response.json() == {
        "md5": digest,
        "status": "ready",
        "open_url": f"/api/library/documents/{digest}/local",
    }
    assert local_response.status_code == 200
    assert local_response.content == b"cached pdf"
    assert local_response.headers["content-type"] == "application/pdf"
    assert local_response.headers["content-disposition"].startswith("inline;")
