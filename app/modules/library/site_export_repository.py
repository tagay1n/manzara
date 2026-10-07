"""PostgreSQL read adapter for the stable static Library export."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.postgres_engine import acquire_postgres_engine, release_postgres_engine

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class LibrarySiteExportRepository:
    """Read one consistent database snapshot without exposing SQL to the SSG."""

    def __init__(self, database_url: str, *, schema: str = "monocorpus") -> None:
        normalized = str(schema or "monocorpus").strip() or "monocorpus"
        if not _SCHEMA_RE.fullmatch(normalized):
            raise ValueError(f"Invalid database schema: {normalized!r}")
        self._engine: Engine = acquire_postgres_engine(
            str(database_url), schema=normalized
        )
        self.schema = normalized

    def dispose(self) -> None:
        release_postgres_engine(self._engine)

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
                candidates = [dict(row) for row in candidates]
                if conn.execute(text("SELECT to_regclass(:table)"), {"table": f'"{self.schema}".catalog_imports'}).scalar():
                    if conn.execute(text(f'SELECT EXISTS(SELECT 1 FROM "{self.schema}".catalog_imports WHERE state=\'active\')')).scalar():
                        aliases = self._attach_catalog_snapshot(conn, candidates)
        return [dict(row) for row in candidates], [dict(row) for row in aliases]

    def _attach_catalog_snapshot(self, conn, candidates):
        """Resolve exact mentions and immutable preview assets in the same snapshot."""
        # The schema was validated at construction, and all values remain bound.
        prefix = f'"{self.schema}".'
        credits = conn.execute(text(f'''SELECT d.md5,c.role,c.role_name,c.resolution,c.entity_id,n.raw_name,
            e.display_name,e.approval,e.status FROM {prefix}catalog_contributions c
            JOIN {prefix}catalog_documents d USING(publication_id)
            JOIN {prefix}catalog_publications p USING(publication_id)
            JOIN {prefix}catalog_names n USING(name_id) LEFT JOIN {prefix}catalog_entities e USING(entity_id)
            WHERE p.inclusion='included' ORDER BY d.md5,c.role,c.position,c.nested_position''')).mappings()
        by_document = {}
        for credit in credits:
            by_document.setdefault(credit["md5"], []).append(dict(credit))
        selected = dict(conn.execute(text(f"SELECT md5,selected FROM {prefix}catalog_documents")).tuples().all())
        previews = conn.execute(text(f'''SELECT DISTINCT ON(md5) request_id,md5,private,source_page_count
            FROM {prefix}catalog_preview_requests WHERE status='ready' ORDER BY md5,request_id DESC''')).mappings()
        preview_rows = {row["md5"]: {**dict(row), "pages": []} for row in previews}
        by_request = {row["request_id"]: row for row in preview_rows.values()}
        for page in conn.execute(text(f'SELECT * FROM {prefix}catalog_preview_pages ORDER BY page_number')).mappings():
            if page["request_id"] in by_request:
                by_request[page["request_id"]]["pages"].append(dict(page))
        for row in candidates:
            row["catalog_contributions"] = by_document.get(row["md5"], [])
            row["catalog_selected"] = selected[row["md5"]]
            row["catalog_preview"] = preview_rows.get(row["md5"])
        return conn.execute(text(f'''WITH roles AS (
            SELECT entity_id,CASE role WHEN 'publisher' THEN 'publisher' ELSE 'personality' END AS entity_type
            FROM {prefix}catalog_entity_roles
            UNION SELECT entity_id,CASE role WHEN 'publisher' THEN 'publisher' ELSE 'personality' END
            FROM {prefix}catalog_contributions WHERE entity_id IS NOT NULL
        ) SELECT e.entity_id AS canonical_id,e.kind AS entity_kind,e.display_name,r.entity_type,
            coalesce(n.raw_name,e.display_name) AS raw_name,'linked' AS decision_status,'active' AS canonical_status
            FROM {prefix}catalog_entities e JOIN roles r USING(entity_id)
            LEFT JOIN {prefix}catalog_aliases a USING(entity_id) LEFT JOIN {prefix}catalog_names n USING(name_id)
            WHERE e.status='active' AND e.approval='confirmed'
            ORDER BY r.entity_type,e.entity_id,n.raw_name''')).mappings().all()


__all__ = ["LibrarySiteExportRepository"]
