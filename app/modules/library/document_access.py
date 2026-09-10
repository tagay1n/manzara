"""Stable document access resolution for Library UI surfaces."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from boto3 import Session
from botocore.config import Config
from sqlalchemy import text

from app.document_storage import (
    DocumentStorageSettings,
    S3ConnectionSettings,
    download_cached_primary_document,
    find_valid_cache_file,
    load_document_storage_settings,
    normalized_extension,
    parse_object_url,
)
from app.modules.runtime_shared_utils import decrypt
from app.postgres_engine import get_postgres_engine
from app.runtime_config import load_runtime_config

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class CachedDocument:
    """One catalog document materialized in the shared local source cache."""

    path: Path
    mime_type: str
    source_name: str


def _create_s3_client(connection: S3ConnectionSettings) -> Any:
    return Session().client(
        service_name="s3",
        aws_access_key_id=connection.access_key_id,
        aws_secret_access_key=connection.secret_access_key,
        endpoint_url=connection.endpoint_url,
        region_name=connection.region_name,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def resolve_stored_document_url(
    row: Mapping[str, Any],
    *,
    settings: DocumentStorageSettings,
    client_factory: Callable[[S3ConnectionSettings], Any] = _create_s3_client,
    expires_seconds: int = 900,
) -> str | None:
    """Resolve a document locator and sign private primary or legacy S3 objects."""
    source = str(row.get("document_url") or row.get("ya_public_url") or "").strip()
    if not source:
        return None
    if source.startswith("enc:"):
        source = decrypt(source, {"encryption_key": settings.encryption_key})

    storage_options = (
        (settings.primary, settings.private_bucket),
        (settings.legacy, settings.legacy_private_bucket),
    )
    for connection, private_bucket in storage_options:
        location = parse_object_url(source, connection.endpoint_url)
        if not location:
            continue
        bucket, key = location
        if bucket != private_bucket:
            return source
        client = client_factory(connection)
        return str(
            client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=max(60, int(expires_seconds)),
            )
        )
    return source


def _load_document_row(state: Any, md5: str) -> Mapping[str, Any] | None:
    schema = str(state.settings.database_schema or "monocorpus")
    if not _SCHEMA_RE.fullmatch(schema):
        raise RuntimeError(f"Invalid database schema: {schema!r}")
    engine = get_postgres_engine(
        state.settings.database_url,
        schema=schema,
        pool_size=state.settings.database_pool_size,
    )
    with engine.connect() as conn:
        return conn.execute(
            text(
                """
                SELECT md5, document_url, ya_public_url, ya_path, mime_type,
                       primary_storage_size, primary_storage_verified_at
                FROM document
                WHERE md5 = :md5
                """
            ),
            {"md5": md5},
        ).mappings().first()


def _cached_document(row: Mapping[str, Any], path: Path) -> CachedDocument:
    source_name = PurePosixPath(str(row.get("ya_path") or "")).name or path.name
    return CachedDocument(
        path=path,
        mime_type=str(row.get("mime_type") or "application/octet-stream"),
        source_name=source_name,
    )


def cache_document_for_local_open(
    state: Any,
    md5: str,
    *,
    client_factory: Callable[[S3ConnectionSettings], Any] = _create_s3_client,
) -> CachedDocument | None:
    """Reuse or download one verified primary document into the shared cache."""
    row = _load_document_row(state, md5)
    if row is None:
        return None
    if not row.get("primary_storage_verified_at"):
        raise ValueError("Document has no verified primary Backblaze object")
    settings = load_document_storage_settings(load_runtime_config())
    if cached := find_valid_cache_file(settings.cache_path, md5):
        return _cached_document(row, cached)
    s3 = client_factory(settings.primary)
    path = download_cached_primary_document(
        settings=settings,
        s3=s3,
        document_url=str(row.get("document_url") or ""),
        expected_md5=md5,
        expected_size=(
            int(row["primary_storage_size"])
            if row.get("primary_storage_size") is not None
            else None
        ),
        extension=normalized_extension(
            str(row.get("ya_path") or ""), str(row.get("mime_type") or "")
        ),
    )
    return _cached_document(row, path)


def resolve_local_cached_document(state: Any, md5: str) -> CachedDocument | None:
    """Resolve a catalog document only when valid bytes exist in the local cache."""
    row = _load_document_row(state, md5)
    if row is None:
        return None
    settings = load_document_storage_settings(load_runtime_config())
    path = find_valid_cache_file(settings.cache_path, md5)
    return _cached_document(row, path) if path else None


def resolve_document_open_url(state: Any, md5: str) -> str | None:
    """Load one document and return its current browser-accessible URL."""
    row = _load_document_row(state, md5)
    if not row:
        return None
    settings = load_document_storage_settings(load_runtime_config())
    return resolve_stored_document_url(row, settings=settings)


__all__ = [
    "CachedDocument",
    "cache_document_for_local_open",
    "resolve_document_open_url",
    "resolve_local_cached_document",
    "resolve_stored_document_url",
]
