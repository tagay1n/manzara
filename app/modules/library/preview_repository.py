"""PostgreSQL persistence for Library PDF preview manifests."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.modules.library.previews import PreviewPage
from app.postgres_engine import acquire_postgres_engine, release_postgres_engine

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STATUSES = {"pending", "processing", "ready", "partial", "failed"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LibraryPreviewRepository:
    """Own preview checkpoints without leaking them into the shared DB layer."""

    def __init__(self, database_url: str, *, schema: str = "monocorpus") -> None:
        normalized_schema = str(schema or "monocorpus").strip() or "monocorpus"
        if not _SCHEMA_RE.fullmatch(normalized_schema):
            raise ValueError(f"Invalid database schema: {normalized_schema!r}")
        self.schema = normalized_schema
        self._engine: Engine = acquire_postgres_engine(
            str(database_url), schema=normalized_schema
        )

    def dispose(self) -> None:
        release_postgres_engine(self._engine)

    def list_candidates(
        self,
        *,
        recipe_version: str,
        endpoint_url: str,
        public_bucket: str,
    ) -> list[dict[str, Any]]:
        """Return applicable PDFs that are not ready for the current recipe."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        d.md5,
                        d.document_url,
                        p.recipe_version,
                        p.source_page_count,
                        p.first_preview_page,
                        p.second_preview_page,
                        p.last_preview_page,
                        p.status,
                        p.error_text
                    FROM document d
                    JOIN metadata m ON m.md5 = d.md5
                    LEFT JOIN library_book_previews p ON p.md5 = d.md5
                    WHERE m.lib IS TRUE
                      AND LOWER(COALESCE(d.mime_type, '')) = 'application/pdf'
                      AND d.sharing_restricted IS NOT TRUE
                      AND d.document_url LIKE :public_prefix
                      AND (
                          p.md5 IS NULL
                          OR p.recipe_version <> :recipe_version
                          OR p.status <> 'ready'
                      )
                    ORDER BY d.md5 ASC
                    """
                ),
                {
                    "recipe_version": str(recipe_version),
                    "public_prefix": (
                        f"{str(endpoint_url).rstrip('/')}/{str(public_bucket)}/%"
                    ),
                },
            ).mappings().all()
        return [self._decode_row(row) for row in rows]

    def start_attempt(
        self,
        md5: str,
        *,
        recipe_version: str,
        run_id: int | None,
    ) -> dict[str, Any]:
        """Mark one book processing and reset page count for a new recipe."""
        now = _utc_now()
        with self._engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO library_book_previews (
                        md5, recipe_version, status, attempt_count,
                        last_run_id, created_at, updated_at
                    )
                    VALUES (
                        :md5, :recipe_version, 'processing', 1,
                        :run_id, :now, :now
                    )
                    ON CONFLICT (md5) DO UPDATE SET
                        source_page_count = CASE
                            WHEN library_book_previews.recipe_version = EXCLUDED.recipe_version
                                THEN library_book_previews.source_page_count
                            ELSE NULL
                        END,
                        first_preview_page = CASE
                            WHEN library_book_previews.recipe_version = EXCLUDED.recipe_version
                                THEN library_book_previews.first_preview_page
                            ELSE NULL
                        END,
                        second_preview_page = CASE
                            WHEN library_book_previews.recipe_version = EXCLUDED.recipe_version
                                THEN library_book_previews.second_preview_page
                            ELSE NULL
                        END,
                        last_preview_page = CASE
                            WHEN library_book_previews.recipe_version = EXCLUDED.recipe_version
                                THEN library_book_previews.last_preview_page
                            ELSE NULL
                        END,
                        recipe_version = EXCLUDED.recipe_version,
                        status = 'processing',
                        attempt_count = library_book_previews.attempt_count + 1,
                        last_run_id = EXCLUDED.last_run_id,
                        error_text = NULL,
                        generated_at = NULL,
                        updated_at = EXCLUDED.updated_at
                    RETURNING *
                    """
                ),
                {
                    "md5": str(md5),
                    "recipe_version": str(recipe_version),
                    "run_id": int(run_id) if run_id is not None else None,
                    "now": now,
                },
            ).mappings().one()
        return self._decode_row(row)

    def checkpoint(
        self,
        md5: str,
        *,
        recipe_version: str,
        source_page_count: int | None,
        selected_pages: list[PreviewPage],
        status: str,
        run_id: int | None,
        error_text: str | None = None,
    ) -> None:
        """Persist one document-level preview checkpoint."""
        normalized_status = str(status or "").strip()
        if normalized_status not in _STATUSES:
            raise ValueError(f"Invalid preview status: {normalized_status!r}")
        now = _utc_now()
        generated_at = now if normalized_status == "ready" else None
        selected_by_role = {page.role: int(page.page_number) for page in selected_pages}
        if len(selected_by_role) != len(selected_pages):
            raise ValueError("Preview page roles must be unique")
        unsupported_roles = set(selected_by_role) - {"first", "second", "last"}
        if unsupported_roles:
            raise ValueError(f"Unsupported preview page roles: {sorted(unsupported_roles)}")
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE library_book_previews
                    SET recipe_version = :recipe_version,
                        source_page_count = :source_page_count,
                        first_preview_page = :first_preview_page,
                        second_preview_page = :second_preview_page,
                        last_preview_page = :last_preview_page,
                        status = :status,
                        last_run_id = :run_id,
                        error_text = :error_text,
                        generated_at = :generated_at,
                        updated_at = :updated_at
                    WHERE md5 = :md5
                    """
                ),
                {
                    "md5": str(md5),
                    "recipe_version": str(recipe_version),
                    "source_page_count": (
                        int(source_page_count) if source_page_count is not None else None
                    ),
                    "first_preview_page": selected_by_role.get("first"),
                    "second_preview_page": selected_by_role.get("second"),
                    "last_preview_page": selected_by_role.get("last"),
                    "status": normalized_status,
                    "run_id": int(run_id) if run_id is not None else None,
                    "error_text": str(error_text or "").strip()[:4000] or None,
                    "generated_at": generated_at,
                    "updated_at": now,
                },
            )
            if int(result.rowcount or 0) != 1:
                raise LookupError(f"Preview row not started for {md5}")

    def get(self, md5: str) -> dict[str, Any] | None:
        """Return one persisted manifest."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM library_book_previews WHERE md5 = :md5"),
                {"md5": str(md5)},
            ).mappings().one_or_none()
        return self._decode_row(row) if row else None

    def is_eligible_pdf(
        self, md5: str, *, endpoint_url: str, public_bucket: str
    ) -> bool:
        """Return whether a document belongs to the preview source set."""
        with self._engine.connect() as conn:
            value = conn.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM document d
                        JOIN metadata m ON m.md5 = d.md5
                        WHERE d.md5 = :md5
                          AND m.lib IS TRUE
                          AND LOWER(COALESCE(d.mime_type, '')) = 'application/pdf'
                          AND d.sharing_restricted IS NOT TRUE
                          AND d.document_url LIKE :public_prefix
                    )
                    """
                ),
                {
                    "md5": str(md5),
                    "public_prefix": (
                        f"{str(endpoint_url).rstrip('/')}/{str(public_bucket)}/%"
                    ),
                },
            ).scalar()
        return bool(value)

    def get_stats(
        self, *, recipe_version: str, endpoint_url: str, public_bucket: str
    ) -> dict[str, Any]:
        """Aggregate current recipe coverage without counting stale rows."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    WITH eligible AS (
                        SELECT d.md5
                        FROM document d
                        JOIN metadata m ON m.md5 = d.md5
                        WHERE m.lib IS TRUE
                          AND LOWER(COALESCE(d.mime_type, '')) = 'application/pdf'
                          AND d.sharing_restricted IS NOT TRUE
                          AND d.document_url LIKE :public_prefix
                    ), current_rows AS (
                        SELECT p.*
                        FROM library_book_previews p
                        JOIN eligible e ON e.md5 = p.md5
                        WHERE p.recipe_version = :recipe_version
                    )
                    SELECT
                        (SELECT COUNT(*) FROM eligible) AS eligible,
                        COUNT(*) FILTER (WHERE status = 'ready') AS ready,
                        COUNT(*) FILTER (WHERE status = 'partial') AS partial,
                        COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                        COALESCE(SUM(
                            CASE WHEN status = 'ready' THEN
                                num_nonnulls(
                                    first_preview_page,
                                    second_preview_page,
                                    last_preview_page
                                )
                            ELSE 0 END
                        ), 0) AS generated_preview_pages,
                        COALESCE(SUM(
                            CASE WHEN status = 'ready' THEN
                                num_nonnulls(
                                    first_preview_page,
                                    second_preview_page,
                                    last_preview_page
                                ) * 2
                            ELSE 0 END
                        ), 0) AS generated_image_objects
                    FROM current_rows
                    """
                ),
                {
                    "recipe_version": str(recipe_version),
                    "public_prefix": (
                        f"{str(endpoint_url).rstrip('/')}/{str(public_bucket)}/%"
                    ),
                },
            ).mappings().one()
        eligible = int(row.get("eligible") or 0)
        ready = int(row.get("ready") or 0)
        partial = int(row.get("partial") or 0)
        failed = int(row.get("failed") or 0)
        return {
            "recipe_version": str(recipe_version),
            "eligible": eligible,
            "ready": ready,
            "pending": max(0, eligible - ready - partial - failed),
            "partial": partial,
            "failed": failed,
            "generated_preview_pages": int(row.get("generated_preview_pages") or 0),
            "generated_image_objects": int(row.get("generated_image_objects") or 0),
        }

    @staticmethod
    def _decode_row(row: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        return payload


__all__ = ["LibraryPreviewRepository"]
