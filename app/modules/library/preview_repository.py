"""PostgreSQL persistence for Library PDF preview manifests."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


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
        self._engine: Engine = create_engine(
            str(database_url),
            connect_args={"options": f"-csearch_path={normalized_schema},public"},
        )

    def dispose(self) -> None:
        """Release pooled database connections."""
        self._engine.dispose()

    def list_candidates(self, *, recipe_version: str) -> list[dict[str, Any]]:
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
                        p.status,
                        p.manifest_json,
                        p.error_text
                    FROM document d
                    JOIN metadata m ON m.md5 = d.md5
                    LEFT JOIN library_book_previews p ON p.md5 = d.md5
                    WHERE m.lib IS TRUE
                      AND LOWER(COALESCE(d.mime_type, '')) = 'application/pdf'
                      AND (
                          p.md5 IS NULL
                          OR p.recipe_version <> :recipe_version
                          OR p.status <> 'ready'
                      )
                    ORDER BY d.md5 ASC
                    """
                ),
                {"recipe_version": str(recipe_version)},
            ).mappings().all()
        return [self._decode_row(row) for row in rows]

    def start_attempt(
        self,
        md5: str,
        *,
        recipe_version: str,
        run_id: int | None,
    ) -> dict[str, Any]:
        """Mark one book processing, resetting a manifest only for a new recipe."""
        now = _utc_now()
        with self._engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO library_book_previews (
                        md5, recipe_version, status, manifest_json, attempt_count,
                        last_run_id, created_at, updated_at
                    )
                    VALUES (
                        :md5, :recipe_version, 'processing', CAST('{}' AS JSONB), 1,
                        :run_id, :now, :now
                    )
                    ON CONFLICT (md5) DO UPDATE SET
                        manifest_json = CASE
                            WHEN library_book_previews.recipe_version = EXCLUDED.recipe_version
                                THEN library_book_previews.manifest_json
                            ELSE CAST('{}' AS JSONB)
                        END,
                        source_page_count = CASE
                            WHEN library_book_previews.recipe_version = EXCLUDED.recipe_version
                                THEN library_book_previews.source_page_count
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
        status: str,
        manifest: Mapping[str, Any],
        run_id: int | None,
        error_text: str | None = None,
    ) -> None:
        """Persist one complete per-variant checkpoint snapshot."""
        normalized_status = str(status or "").strip()
        if normalized_status not in _STATUSES:
            raise ValueError(f"Invalid preview status: {normalized_status!r}")
        now = _utc_now()
        generated_at = now if normalized_status == "ready" else None
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE library_book_previews
                    SET recipe_version = :recipe_version,
                        source_page_count = :source_page_count,
                        status = :status,
                        manifest_json = CAST(:manifest_json AS JSONB),
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
                    "status": normalized_status,
                    "manifest_json": json.dumps(dict(manifest), ensure_ascii=False),
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

    def is_eligible_pdf(self, md5: str) -> bool:
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
                    )
                    """
                ),
                {"md5": str(md5)},
            ).scalar()
        return bool(value)

    def get_stats(self, *, recipe_version: str) -> dict[str, Any]:
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
                    ), current_rows AS (
                        SELECT p.*
                        FROM library_book_previews p
                        JOIN eligible e ON e.md5 = p.md5
                        WHERE p.recipe_version = :recipe_version
                    ), page_objects AS (
                        SELECT COUNT(*) AS count
                        FROM current_rows p
                        CROSS JOIN LATERAL jsonb_each(p.manifest_json) AS page(role, payload)
                        WHERE jsonb_typeof(page.payload) = 'object'
                          AND EXISTS (
                              SELECT 1
                              FROM jsonb_each(COALESCE(page.payload->'variants', '{}'::jsonb))
                          )
                    ), image_objects AS (
                        SELECT COUNT(*) AS count
                        FROM current_rows p
                        CROSS JOIN LATERAL jsonb_each(p.manifest_json) AS page(role, payload)
                        CROSS JOIN LATERAL jsonb_each(
                            COALESCE(page.payload->'variants', '{}'::jsonb)
                        ) AS variant(name, payload)
                        WHERE COALESCE(variant.payload->>'key', '') <> ''
                    )
                    SELECT
                        (SELECT COUNT(*) FROM eligible) AS eligible,
                        COUNT(*) FILTER (WHERE status = 'ready') AS ready,
                        COUNT(*) FILTER (WHERE status = 'partial') AS partial,
                        COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                        (SELECT count FROM page_objects) AS generated_preview_pages,
                        (SELECT count FROM image_objects) AS generated_image_objects
                    FROM current_rows
                    """
                ),
                {"recipe_version": str(recipe_version)},
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
        manifest = payload.pop("manifest_json", {})
        if isinstance(manifest, str):
            try:
                manifest = json.loads(manifest)
            except json.JSONDecodeError:
                manifest = {}
        payload["manifest"] = manifest if isinstance(manifest, dict) else {}
        return payload


__all__ = ["LibraryPreviewRepository"]
