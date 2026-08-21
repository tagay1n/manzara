"""PostgreSQL source and checkpoints for primary document uploads."""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import create_engine, text


class PostgresDocumentSyncRepository:
    """Own the pending document queue and verified storage checkpoints."""

    def __init__(self, database_url: str, *, schema: str) -> None:
        self.engine = create_engine(
            database_url,
            connect_args={"options": f"-csearch_path={schema},public"},
        )

    def dispose(self) -> None:
        self.engine.dispose()

    def list_pending_documents(self) -> list[dict[str, Any]]:
        """Return null-URL rows while detecting any ambiguous MD5 identity."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    WITH duplicate_md5 AS (
                        SELECT md5
                        FROM document
                        WHERE md5 IS NOT NULL
                        GROUP BY md5
                        HAVING COUNT(*) > 1
                    )
                    SELECT md5, mime_type, ya_path, sharing_restricted,
                           document_url, primary_storage_size,
                           primary_storage_etag, primary_storage_verified_at
                    FROM document
                    WHERE document_url IS NULL
                       OR BTRIM(document_url) = ''
                       OR md5 IN (SELECT md5 FROM duplicate_md5)
                    ORDER BY ya_path NULLS LAST, md5
                    """
                )
            ).mappings().all()

        seen: set[str] = set()
        pending: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            md5 = str(item.get("md5") or "").strip().lower()
            if not md5:
                raise RuntimeError("Pending document row has no MD5")
            if md5 in seen:
                raise RuntimeError(
                    f"Duplicate document MD5 {md5}; refusing upload"
                )
            seen.add(md5)
            if not str(item.get("document_url") or "").strip():
                item["md5"] = md5
                pending.append(item)
        return pending

    def count_pending_documents(self) -> int:
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM document
                        WHERE document_url IS NULL OR BTRIM(document_url) = ''
                        """
                    )
                ).scalar_one()
            )

    def save_storage_checkpoint(
        self,
        md5: str,
        payload: Mapping[str, Any],
    ) -> bool:
        """Commit a verified primary-storage checkpoint to one pending row."""
        values = {
            "md5": str(md5).strip().lower(),
            "document_url": payload.get("document_url"),
            "primary_storage_size": payload.get("primary_storage_size"),
            "primary_storage_etag": payload.get("primary_storage_etag"),
            "primary_storage_verified_at": payload.get(
                "primary_storage_verified_at"
            ),
        }
        with self.engine.begin() as conn:
            updated = conn.execute(
                text(
                    """
                    UPDATE document SET
                        document_url = :document_url,
                        primary_storage_size = :primary_storage_size,
                        primary_storage_etag = :primary_storage_etag,
                        primary_storage_verified_at = :primary_storage_verified_at
                    WHERE md5 = :md5
                      AND (document_url IS NULL OR BTRIM(document_url) = '')
                    """
                ),
                values,
            )
            if updated.rowcount > 1:
                raise RuntimeError(
                    f"Document MD5 {values['md5']} matched {updated.rowcount} rows; "
                    "refusing ambiguous checkpoint"
                )
            return updated.rowcount == 1


__all__ = ["PostgresDocumentSyncRepository"]
