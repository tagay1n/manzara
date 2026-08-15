"""PostgreSQL catalog and cleanup state for guarded monocorpus synchronization."""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import text

from app.modules.library.document_cleanup_repository import DocumentCleanupRepository


class MonocorpusSyncRepository(DocumentCleanupRepository):
    """Extend cleanup persistence with catalog synchronization operations."""

    def list_documents(self) -> dict[str, dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT md5, mime_type, ya_path, ya_public_url, ya_public_key,
                           ya_resource_id, language, "full", sharing_restricted,
                           document_url, content_url, upstream_meta_url
                    FROM document
                    WHERE md5 IS NOT NULL
                    """
                )
            ).mappings()
            documents: dict[str, dict[str, Any]] = {}
            for row in rows:
                md5 = str(row["md5"]).strip().lower()
                if md5 in documents:
                    raise RuntimeError(f"Duplicate document MD5 {md5}; refusing sync")
                documents[md5] = dict(row)
            return documents

    def list_active_cleanup(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT cleanup_id, scope, action, reason, md5,
                               source_resource_id, source_path, target_path,
                               status, phase, evidence_json, attempts
                        FROM document_cleanup_queue
                        WHERE status IN ('planned', 'running', 'failed')
                        ORDER BY cleanup_id
                        """
                    )
                ).mappings()
            ]

    def mark_cleanup_running(self, cleanup_id: int, *, run_id: int, phase: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE document_cleanup_queue SET status='running', phase=:phase,
                        run_id=:run_id, attempts=attempts+1, last_error=NULL,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE cleanup_id=:cleanup_id
                    """
                ),
                {"cleanup_id": cleanup_id, "run_id": run_id, "phase": phase},
            )

    def mark_cleanup_phase(self, cleanup_id: int, phase: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE document_cleanup_queue SET phase=:phase,
                        updated_at=CURRENT_TIMESTAMP WHERE cleanup_id=:cleanup_id
                    """
                ),
                {"cleanup_id": cleanup_id, "phase": phase},
            )

    def mark_cleanup_completed(self, cleanup_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE document_cleanup_queue SET status='completed', phase='completed',
                        last_error=NULL, completed_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP WHERE cleanup_id=:cleanup_id
                    """
                ),
                {"cleanup_id": cleanup_id},
            )

    def mark_cleanup_failed(self, cleanup_id: int, error: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE document_cleanup_queue SET status='failed',
                        last_error=:error, updated_at=CURRENT_TIMESTAMP
                    WHERE cleanup_id=:cleanup_id
                    """
                ),
                {"cleanup_id": cleanup_id, "error": str(error)[:4000]},
            )

    def mark_cleanup_canceled(self, cleanup_id: int, reason: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE document_cleanup_queue SET status='canceled',
                        phase='canceled', last_error=NULL,
                        evidence_json=evidence_json || jsonb_build_object(
                            'cancellation', :reason
                        ),
                        completed_at=COALESCE(completed_at, CURRENT_TIMESTAMP),
                        updated_at=CURRENT_TIMESTAMP
                    WHERE cleanup_id=:cleanup_id
                    """
                ),
                {"cleanup_id": cleanup_id, "reason": str(reason)[:1000]},
            )

    def save_discovered_document(self, payload: Mapping[str, Any]) -> bool:
        """Update Yandex catalog fields without erasing unrelated metadata."""
        values = dict(payload)
        with self.engine.begin() as conn:
            updated = conn.execute(
                text(
                    """
                    UPDATE document SET
                        mime_type=:mime_type, ya_path=:ya_path,
                        ya_public_url=COALESCE(:ya_public_url, ya_public_url),
                        ya_public_key=COALESCE(:ya_public_key, ya_public_key),
                        ya_resource_id=COALESCE(:ya_resource_id, ya_resource_id),
                        "full"=:full, sharing_restricted=:sharing_restricted
                    WHERE md5=:md5
                    """
                ),
                values,
            )
            if updated.rowcount > 1:
                raise RuntimeError(
                    f"Document MD5 {values['md5']} matched {updated.rowcount} rows"
                )
            if updated.rowcount == 1:
                return False
            conn.execute(
                text(
                    """
                    INSERT INTO document (
                        md5, mime_type, ya_path, ya_public_url, ya_public_key,
                        ya_resource_id, "full", sharing_restricted
                    ) VALUES (
                        :md5, :mime_type, :ya_path, :ya_public_url, :ya_public_key,
                        :ya_resource_id, :full, :sharing_restricted
                    )
                    """
                ),
                values,
            )
            return True

    def delete_document_state(self, md5: str) -> None:
        """Delete one document and known dependent state in one transaction."""
        dependent_tables = (
            "library_book_previews",
            "library_collection_proposal_items",
            "library_collection_items",
            "library_collection_document_features",
            "document_crh",
            "isbn_keep_many",
            "metadata",
        )
        with self.engine.begin() as conn:
            for table_name in dependent_tables:
                exists = conn.execute(
                    text("SELECT to_regclass(:name) IS NOT NULL"),
                    {"name": table_name},
                ).scalar_one()
                if exists:
                    conn.execute(
                        text(f'DELETE FROM "{table_name}" WHERE md5=:md5'),
                        {"md5": md5},
                    )
            deleted = conn.execute(
                text("DELETE FROM document WHERE md5=:md5"), {"md5": md5}
            )
            if deleted.rowcount > 1:
                raise RuntimeError(f"Document MD5 {md5} deleted multiple rows")


__all__ = ["MonocorpusSyncRepository"]
