"""Library-owned exact attention counts."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.document_storage import load_document_storage_settings
from app.modules.library.previews import PREVIEW_RECIPE_VERSION
from app.postgres_engine import acquire_postgres_engine, release_postgres_engine
from app.runtime_config import load_runtime_config


def load_library_queue_signals(settings: Any, _source_event_id: int) -> list[dict[str, Any]]:
    engine = acquire_postgres_engine(
        settings.database_url, schema=settings.database_schema
    )
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM document_cleanup_queue
                       WHERE status IN ('planned','running','failed')) cleanup_plans,
                      (SELECT COUNT(*) FROM library_isbn_duplicate_reviews
                       WHERE status='pending') cleanup_reviews,
                      (SELECT COUNT(*) FROM library_collection_proposals
                       WHERE status='queued_validation') collection_validation,
                      (SELECT COUNT(*) FROM library_collection_proposals
                       WHERE status='review_ready') collection_reviews
                    """
                )
            ).mappings().one()
    finally:
        release_postgres_engine(engine)
    specs = [
        (
            "library.cleanup.plans", "library.prepare_document_cleanup", "maintenance",
            "document-cleanup", "cleanup_plans", "Cleanup plans ready",
            "/library/document-cleanup",
        ),
        (
            "library.cleanup.reviews", "library.prepare_document_cleanup", "maintenance",
            "document-cleanup", "cleanup_reviews", "Cleanup reviews pending",
            "/library/document-cleanup",
        ),
        (
            "library.collections.validation", "library.collection_validate", "collections",
            "collections", "collection_validation", "Collections awaiting validation",
            "/library/collections",
        ),
        (
            "library.collections.reviews", None, "collections", "collections",
            "collection_reviews", "Collection reviews pending", "/library/collections",
        ),
    ]
    return [
        {
            "signal_id": signal_id,
            "task_id": task_id,
            "panel_id": panel_id,
            "section_id": section_id,
            "kind": "count",
            "count": int(row[key] or 0),
            "label": label,
            "href": href,
        }
        for signal_id, task_id, panel_id, section_id, key, label, href in specs
    ]


def load_preview_signals(settings: Any, _source_event_id: int) -> list[dict[str, Any]]:
    storage = load_document_storage_settings(load_runtime_config())
    public_prefix = f"{storage.primary.endpoint_url.rstrip('/')}/{storage.public_bucket}/%"
    engine = acquire_postgres_engine(
        settings.database_url, schema=settings.database_schema
    )
    try:
        with engine.connect() as conn:
            count = conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM document d
                    JOIN metadata m ON m.md5=d.md5
                    LEFT JOIN library_book_previews p ON p.md5=d.md5
                    WHERE m.lib IS TRUE
                      AND LOWER(COALESCE(d.mime_type,''))='application/pdf'
                      AND d.sharing_restricted IS NOT TRUE
                      AND d.document_url LIKE :public_prefix
                      AND (p.md5 IS NULL OR p.recipe_version<>:recipe
                           OR p.status<>'ready')
                    """
                ),
                {"public_prefix": public_prefix, "recipe": PREVIEW_RECIPE_VERSION},
            ).scalar_one()
    finally:
        release_postgres_engine(engine)
    return [
        {
            "signal_id": "library.previews.pending",
            "task_id": "library.generate_book_previews",
            "panel_id": "library",
            "section_id": "overview",
            "kind": "count",
            "count": int(count or 0),
            "label": "Book previews available",
            "href": "/tasks/library.generate_book_previews",
        }
    ]


__all__ = ["load_library_queue_signals", "load_preview_signals"]
