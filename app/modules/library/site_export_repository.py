"""PostgreSQL read adapter for the stable static Library export."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class LibrarySiteExportRepository:
    """Read one consistent database snapshot without exposing SQL to the SSG."""

    def __init__(self, database_url: str, *, schema: str = "monocorpus") -> None:
        normalized = str(schema or "monocorpus").strip() or "monocorpus"
        if not _SCHEMA_RE.fullmatch(normalized):
            raise ValueError(f"Invalid database schema: {normalized!r}")
        self._engine: Engine = create_engine(
            str(database_url),
            connect_args={"options": f"-csearch_path={normalized},public"},
        )

    def dispose(self) -> None:
        self._engine.dispose()

    def load_snapshot(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Load candidates and reviewed aliases in one repeatable-read transaction."""
        with self._engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as conn:
            with conn.begin():
                candidates = conn.execute(
                    text(
                        """
                        SELECT
                            d.md5,
                            d.mime_type,
                            d."full",
                            d.sharing_restricted,
                            d.document_url,
                            d.content_url,
                            d.primary_storage_size,
                            d.primary_storage_verified_at,
                            m.schema_org,
                            m.classification_id,
                            c.ddc,
                            c.path_en,
                            c.path_tt,
                            collection.collection_id,
                            collection.title AS collection_title,
                            collection.include_in_library AS collection_include,
                            preview.recipe_version AS preview_recipe_version,
                            preview.status AS preview_status,
                            preview.source_page_count,
                            preview.first_preview_page,
                            preview.second_preview_page,
                            preview.last_preview_page,
                            EXISTS (
                                SELECT 1
                                FROM document_cleanup_queue cleanup
                                WHERE cleanup.md5 = d.md5
                                  AND cleanup.scope = 'document'
                                  AND cleanup.reason = 'corrupted'
                                  AND cleanup.status IN ('planned', 'running', 'failed')
                            ) AS has_active_corruption
                        FROM document d
                        JOIN metadata m ON m.md5 = d.md5
                        LEFT JOIN classification c ON c.id = m.classification_id
                        LEFT JOIN library_collection_items collection_item
                          ON collection_item.md5 = d.md5
                        LEFT JOIN library_collections collection
                          ON collection.collection_id = collection_item.collection_id
                        LEFT JOIN library_book_previews preview
                          ON preview.md5 = d.md5
                        WHERE m.lib IS TRUE
                        ORDER BY d.md5
                        """
                    )
                ).mappings().all()
                aliases = conn.execute(
                    text(
                        """
                        SELECT
                            alias.entity_type,
                            alias.raw_name,
                            alias.decision_status,
                            COALESCE(target.canonical_id, canonical.canonical_id)
                                AS canonical_id,
                            COALESCE(target.display_name, canonical.display_name)
                                AS display_name,
                            COALESCE(target.status, canonical.status)
                                AS canonical_status,
                            NULL::BIGINT AS merged_into_id
                        FROM normalization_aliases alias
                        JOIN normalization_canonicals canonical
                          ON canonical.canonical_id = alias.canonical_id
                        LEFT JOIN normalization_canonicals target
                          ON target.canonical_id = canonical.merged_into_id
                        WHERE alias.decision_status = 'linked'
                          AND COALESCE(target.status, canonical.status) = 'active'
                          AND canonical.entity_type IN ('personality', 'publisher')
                        ORDER BY alias.entity_type, alias.raw_name
                        """
                    )
                ).mappings().all()
        return [dict(row) for row in candidates], [dict(row) for row in aliases]


__all__ = ["LibrarySiteExportRepository"]
