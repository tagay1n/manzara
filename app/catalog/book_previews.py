
from app.postgres_engine import configured_timeout_sql
from app.settings import configured_schema

"""Catalog-native eligibility and guarded publication for automatic PDF previews."""

import re

from sqlalchemy import inspect, select, text

from app.catalog.contracts import CatalogConflict, document_md5, integer
from app.catalog.repository import CatalogRepository

SOURCES = """
    SELECT d.md5, d.publication_id, d.revision AS document_revision,
           p.revision AS publication_revision, s.location_id,
           s.revision AS primary_revision, s.locator, s.size, s.etag, s.verified_at,
           latest.request_id, latest.status AS request_status, latest.lease_until
    FROM catalog_documents d
    JOIN catalog_publications p USING(publication_id)
    JOIN catalog_locations s ON s.md5=d.md5 AND s.provider='s3' AND s.purpose='primary'
    LEFT JOIN LATERAL (
        SELECT r.request_id, r.status, r.lease_until
        FROM catalog_preview_requests r WHERE r.md5=d.md5 AND r.recipe=:recipe
        ORDER BY r.request_id DESC LIMIT 1
    ) latest ON TRUE
    WHERE p.inclusion='included' AND p.merged_into_id IS NULL
      AND d.mime_type='application/pdf' AND d.complete IS TRUE AND d.restricted IS FALSE
      AND NULLIF(BTRIM(s.locator),'') IS NOT NULL AND s.size IS NOT NULL
      AND s.verified_at IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM document_cleanup_queue q WHERE q.md5=d.md5
          AND q.scope='document' AND q.status IN ('planned','running','failed'))
"""
PENDING = """
      AND NOT EXISTS (
          SELECT 1 FROM catalog_preview_requests r WHERE r.md5=d.md5
          AND r.recipe=:recipe AND r.status='ready' AND r.private IS FALSE)
      AND NOT EXISTS (
          SELECT 1 FROM catalog_preview_requests r WHERE r.md5=d.md5
          AND r.status='processing' AND r.lease_until>CURRENT_TIMESTAMP)
      AND (:retry OR latest.status IS DISTINCT FROM 'failed')
"""
SNAPSHOT_FIELDS = (
    "publication_id", "document_revision", "publication_revision", "location_id",
    "primary_revision", "locator", "size", "etag", "verified_at",
)


class BookPreviewCatalogStore:
    def __init__(self, engine, *, schema):
        self.engine = engine
        self.schema = schema
        self.catalog = CatalogRepository(engine, schema=schema)

    def preflight(self, *, recipe):
        """Read deployed columns, key constraints and the actual candidate envelope."""
        required = {
            "catalog_documents": {"md5", "publication_id", "revision", "updated_at", "mime_type", "complete", "restricted", "selected", "content_extraction_method", "meta_extraction_method"},
            "catalog_publications": {"publication_id", "revision", "inclusion", "merged_into_id"},
            "catalog_locations": {"location_id", "md5", "provider", "purpose", "revision", "locator", "size", "etag", "verified_at"},
            "catalog_preview_requests": {"request_id", "md5", "recipe", "idempotency_key", "status", "private", "actor", "claim_token", "lease_until", "source_page_count", "created_at"},
            "catalog_preview_pages": {"request_id", "role", "page_number", "small_key", "large_key"},
            "catalog_revisions": {"record_kind", "record_key", "actor", "before", "after"},
            "document_cleanup_queue": {"md5", "scope", "status"},
        }
        if self.engine.pool.size() < 2:
            raise RuntimeError("Book previews require database_pool_size of at least 2")
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text(configured_timeout_sql("statement_timeout", "preflight_timeout_seconds")))
            inspector = inspect(conn)
            tables = set(inspector.get_table_names(schema=self.schema))
            for relation, columns in required.items():
                if relation not in tables:
                    raise RuntimeError(f"Book previews require {relation}; apply migrations separately")
                present = {item['name'] for item in inspector.get_columns(relation, schema=self.schema)}
                if columns - present:
                    raise RuntimeError(f"Book previews require {relation} columns: {sorted(columns - present)}")
            version_schema = configured_schema("migration_version_schema")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", version_schema):
                raise ValueError("Invalid migration version schema")
            revision = str(conn.execute(text(
                f'SELECT version_num FROM "{version_schema}".alembic_version_manzara'
            )).scalar_one())
            if not re.fullmatch(r"\d{8}_\d{4}", revision) or int(revision.split('_')[1]) < 62:
                raise RuntimeError("Book previews require catalog revision 20261008_0062 or later")
            for relation, key in (("catalog_documents", ["md5"]),
                                  ("catalog_preview_requests", ["request_id"]),
                                  ("catalog_preview_pages", ["request_id", "role"])):
                if inspector.get_pk_constraint(relation, schema=self.schema)['constrained_columns'] != key:
                    raise RuntimeError(f"Book previews require {relation} primary key {key}")
            for relation, key in (("catalog_locations", {"md5", "provider", "purpose"}),
                                  ("catalog_preview_requests", {"md5", "idempotency_key"})):
                unique = inspector.get_unique_constraints(relation, schema=self.schema)
                if not any(set(item['column_names']) == key for item in unique):
                    raise RuntimeError(f"Book previews require {relation} unique key {sorted(key)}")
            for relation, parent, key in (
                ("catalog_preview_requests", "catalog_documents", "md5"),
                ("catalog_preview_pages", "catalog_preview_requests", "request_id"),
            ):
                foreign_keys = inspector.get_foreign_keys(relation, schema=self.schema)
                if not any(item['referred_table'] == parent and item['constrained_columns'] == [key]
                           and item['options'].get('ondelete') == 'CASCADE' for item in foreign_keys):
                    raise RuntimeError(f"Book previews require cascading ownership for {relation}")
            conn.execute(text(SOURCES + PENDING + " ORDER BY d.md5 LIMIT 1"),
                         {"recipe": recipe, "retry": False}).mappings().all()

    def list_candidates(self, *, recipe, limit=None, only_md5s=(), retry_known_failures=False):
        if not isinstance(retry_known_failures, bool):
            raise ValueError("retry_known_failures must be a boolean")
        if limit is not None:
            integer(limit, "limit")
        parameters = {"recipe": recipe, "retry": retry_known_failures}
        query = SOURCES + PENDING
        if only_md5s:
            parameters["md5s"] = [document_md5(md5) for md5 in only_md5s]
            query += " AND d.md5=ANY(:md5s)"
        query += " ORDER BY d.md5"
        if limit is not None:
            query += " LIMIT :limit"
            parameters["limit"] = limit
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(text(query), parameters).mappings()]

    def check_source(self, expected, *, recipe, conn=None):
        if conn is None:
            with self.engine.connect() as connection:
                return self.check_source(expected, recipe=recipe, conn=connection)
        row = conn.execute(text(SOURCES + " AND d.md5=:md5"),
                           {"recipe": recipe, "md5": expected["md5"]}).mappings().one_or_none()
        if row is None or any(row[field] != expected[field] for field in SNAPSHOT_FIELDS):
            raise CatalogConflict(f"Preview source or eligibility changed for {expected['md5']}")
        return dict(row)

    def prepare_request(self, expected, *, recipe, actor, run_id, retry_known_failures):
        """Called under the document operation lock; never creates an unrelated request."""
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ WRITE"))
            self.catalog._record(conn, "document", expected["md5"])
            self.check_source(expected, recipe=recipe, conn=conn)
            row = conn.execute(text(SOURCES + PENDING + " AND d.md5=:md5"), {
                "recipe": recipe, "md5": expected["md5"], "retry": retry_known_failures,
            }).mappings().one_or_none()
            if row is None:
                return None
            if row["request_status"] in {"pending", "processing"}:
                return row["request_id"]
            table = self.catalog.table("preview_requests")
            request = dict(conn.execute(table.insert().values(
                md5=expected["md5"], recipe=recipe, actor=actor, private=False,
                idempotency_key=f"recipe:{recipe}:run:{run_id}",
            ).returning(table)).mappings().one())
            self.catalog._audit(conn, "preview", request["request_id"], None, request, actor)
            return request["request_id"]

    def finish(self, expected, request, *, pages, source_page_count, actor):
        """Recheck source and publication in the same transaction as success."""
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ WRITE"))
            self.catalog._record(conn, "document", expected["md5"])
            publications = self.catalog.table("publications")
            conn.execute(select(publications.c.publication_id).where(
                publications.c.publication_id == expected["publication_id"]
            ).with_for_update()).scalar_one_or_none()
            locations = self.catalog.table("locations")
            conn.execute(select(locations.c.location_id).where(
                locations.c.location_id == expected["location_id"]
            ).with_for_update()).scalar_one_or_none()
            self.check_source(expected, recipe=request["recipe"], conn=conn)
            return self.catalog.finish_preview(
                request["request_id"], request["claim_token"], pages=pages,
                source_page_count=source_page_count, actor=actor, conn=conn,
            )
