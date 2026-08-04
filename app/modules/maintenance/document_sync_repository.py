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
                           ya_resource_id, language, "full", sharing_restricted,
                           document_url, content_url, upstream_meta_url,
                           primary_storage_size, primary_storage_etag,
                           primary_storage_verified_at
                    FROM document
                    """
                )
            ).mappings().all()
        documents: dict[str, dict[str, Any]] = {}
        for row in rows:
            md5 = str(row.get("md5") or "").strip().lower()
            if not md5:
                raise RuntimeError("Document row has no MD5; refusing synchronization")
            if md5 in documents:
                raise RuntimeError(
                    f"Duplicate document MD5 {md5}; refusing synchronization"
                )
            documents[md5] = dict(row)
        return documents

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
        with self.engine.begin() as conn:
            updated = conn.execute(
                text(
                    """
                    UPDATE document SET
                        mime_type = :mime_type,
                        ya_path = :ya_path,
                        ya_public_url = :ya_public_url,
                        ya_public_key = :ya_public_key,
                        ya_resource_id = :ya_resource_id,
                        "full" = :full,
                        sharing_restricted = :sharing_restricted,
                        document_url = :document_url,
                        upstream_meta_url = COALESCE(
                            document.upstream_meta_url,
                            :upstream_meta_url
                        ),
                        primary_storage_size = :primary_storage_size,
                        primary_storage_etag = :primary_storage_etag,
                        primary_storage_verified_at = :primary_storage_verified_at
                    WHERE md5 = :md5
                    """
                ),
                dict(payload),
            )
            if updated.rowcount > 1:
                raise RuntimeError(
                    f"Document MD5 {payload['md5']} matched {updated.rowcount} rows; "
                    "refusing ambiguous update"
                )
            if updated.rowcount == 1:
                return False
            inserted = conn.execute(
                text(
                    """
                    INSERT INTO document (
                        md5, mime_type, ya_path, ya_public_url, ya_public_key,
                        ya_resource_id, language, "full", sharing_restricted,
                        document_url, upstream_meta_url, primary_storage_size,
                        primary_storage_etag, primary_storage_verified_at
                    ) VALUES (
                        :md5, :mime_type, :ya_path, :ya_public_url, :ya_public_key,
                        :ya_resource_id, NULL, :full, :sharing_restricted,
                        :document_url, :upstream_meta_url, :primary_storage_size,
                        :primary_storage_etag, :primary_storage_verified_at
                    )
                    """
                ),
                dict(payload),
            )
            if inserted.rowcount != 1:
                raise RuntimeError(
                    f"Document MD5 {payload['md5']} insert affected "
                    f"{inserted.rowcount} rows"
                )
        return True


__all__ = ["PostgresDocumentSyncRepository"]
