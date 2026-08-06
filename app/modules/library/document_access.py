"""Stable document access resolution for Library UI surfaces."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import re
from typing import Any

from boto3 import Session
from botocore.config import Config
from sqlalchemy import create_engine, text

from app.document_storage import (
    DocumentStorageSettings,
    S3ConnectionSettings,
    load_document_storage_settings,
    parse_object_url,
)
from app.modules.runtime_shared_utils import decrypt
from app.runtime_config import load_runtime_config


_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


def resolve_document_open_url(state: Any, md5: str) -> str | None:
    """Load one document and return its current browser-accessible URL."""
    schema = str(state.settings.database_schema or "monocorpus")
    if not _SCHEMA_RE.fullmatch(schema):
        raise RuntimeError(f"Invalid database schema: {schema!r}")
    engine = create_engine(state.settings.database_url)
    try:
        with engine.connect() as conn:
            conn.execute(text(f'SET search_path TO "{schema}", public'))
            row = conn.execute(
                text(
                    """
                    SELECT document_url, ya_public_url, primary_storage_verified_at
                    FROM document
                    WHERE md5 = :md5
                    """
                ),
                {"md5": md5},
            ).mappings().first()
        if not row:
            return None
        settings = load_document_storage_settings(load_runtime_config())
        return resolve_stored_document_url(row, settings=settings)
    finally:
        engine.dispose()


__all__ = ["resolve_document_open_url", "resolve_stored_document_url"]
