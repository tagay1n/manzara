"""PostgreSQL persistence for primary document-storage synchronization."""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import create_engine, text

from app.document_storage import object_url


class PostgresDocumentSyncRepository:
    """Own document storage reads and verification checkpoints."""

    def __init__(self, database_url: str, *, schema: str) -> None:
        self.engine = create_engine(
            database_url,
            connect_args={"options": f"-csearch_path={schema},public"},
        )

    def dispose(self) -> None:
        self.engine.dispose()

    def list_documents(self) -> dict[str, dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT md5, mime_type, ya_path, ya_public_url, ya_public_key,
                           ya_resource_id, language, full, sharing_restricted,
                           document_url, content_url, upstream_meta_url,
                           primary_storage_size, primary_storage_etag,
                           primary_storage_verified_at
                    FROM document
                    """
                )
            ).mappings().all()
        return {str(row["md5"]).lower(): dict(row) for row in rows}

    def list_upstream_metadata(
        self,
        s3: Any,
        bucket: str,
        endpoint_url: str,
    ) -> dict[str, str]:
        paginator = s3.get_paginator("list_objects_v2")
        return {
            key.removesuffix(".zip"): object_url(endpoint_url, bucket, key)
            for page in paginator.paginate(Bucket=bucket)
            for item in page.get("Contents", [])
            if (key := str(item.get("Key") or "")).endswith(".zip")
        }

    def save_verified_document(self, payload: Mapping[str, Any]) -> bool:
        created = bool(payload.get("created"))
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO document (
                        md5, mime_type, ya_path, ya_public_url, ya_public_key,
                        ya_resource_id, language, full, sharing_restricted,
                        document_url, upstream_meta_url, primary_storage_size,
                        primary_storage_etag, primary_storage_verified_at
                    ) VALUES (
                        :md5, :mime_type, :ya_path, :ya_public_url, :ya_public_key,
                        :ya_resource_id, NULL, :full, :sharing_restricted,
                        :document_url, :upstream_meta_url, :primary_storage_size,
                        :primary_storage_etag, :primary_storage_verified_at
                    )
                    ON CONFLICT (md5) DO UPDATE SET
                        mime_type = EXCLUDED.mime_type,
                        ya_path = EXCLUDED.ya_path,
                        ya_public_url = EXCLUDED.ya_public_url,
                        ya_public_key = EXCLUDED.ya_public_key,
                        ya_resource_id = EXCLUDED.ya_resource_id,
                        full = EXCLUDED.full,
                        sharing_restricted = EXCLUDED.sharing_restricted,
                        document_url = EXCLUDED.document_url,
                        upstream_meta_url = COALESCE(
                            document.upstream_meta_url,
                            EXCLUDED.upstream_meta_url
                        ),
                        primary_storage_size = EXCLUDED.primary_storage_size,
                        primary_storage_etag = EXCLUDED.primary_storage_etag,
                        primary_storage_verified_at = EXCLUDED.primary_storage_verified_at
                    """
                ),
                dict(payload),
            )
        return created


__all__ = ["PostgresDocumentSyncRepository"]
