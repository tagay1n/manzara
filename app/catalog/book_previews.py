"""Catalog-native eligibility and guarded publication for automatic PDF previews."""

import json
import re
import uuid
from pathlib import Path

from sqlalchemy import text

from app.catalog.contracts import boolean, document_md5, integer, nonblank
from app.catalog.previews import preview_command, validate_pages
from app.postgres_engine import configured_timeout_sql
from app.settings import configured_schema

PREVIEW_BATCH_SIZE = 200

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

    def preflight(self, *, recipe):
        """Read deployed columns, key constraints and the actual candidate envelope."""
        required = {
            "catalog_documents": {"md5", "publication_id", "revision", "updated_at", "mime_type", "complete", "restricted", "selected", "content_extraction_method", "meta_extraction_method"},
            "catalog_publications": {"publication_id", "revision", "inclusion", "merged_into_id"},
            "catalog_locations": {"location_id", "md5", "provider", "purpose", "revision", "locator", "size", "etag", "verified_at"},
            "catalog_preview_requests": {"request_id", "md5", "recipe", "idempotency_key", "status", "private", "actor", "claim_token", "lease_until", "source_page_count", "created_at"},
            "catalog_preview_pages": {"request_id", "role", "page_number", "small_key", "large_key"},
            "document_cleanup_queue": {"md5", "scope", "status"},
        }
        if self.engine.pool.size() < 2:
            raise RuntimeError("Book previews require database_pool_size of at least 2")
        version_schema = configured_schema("migration_version_schema")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", version_schema):
            raise ValueError("Invalid migration version schema")
        keys = [
            {"relation": relation, "kind": kind, "columns": columns}
            for relation, kind, columns in (
                ("catalog_documents", "p", ["md5"]),
                ("catalog_preview_requests", "p", ["request_id"]),
                ("catalog_preview_pages", "p", ["request_id", "role"]),
                ("catalog_locations", "u", ["md5", "provider", "purpose"]),
                ("catalog_preview_requests", "u", ["md5", "idempotency_key"]),
            )
        ]
        foreign_keys = [
            {"relation": relation, "parent": parent, "column_name": column}
            for relation, parent, column in (
                ("catalog_preview_requests", "catalog_documents", "md5"),
                ("catalog_preview_pages", "catalog_preview_requests", "request_id"),
            )
        ]
        query = Path(__file__).with_name("book_preview_preflight.sql").read_text(encoding="utf-8")
        query = query.replace("{{version_schema}}", version_schema).replace("{{candidate_query}}", SOURCES + PENDING)
        # Schema/key aggregation stays in PostgreSQL, rather than separate
        # inspector queries for every table and constraint.
        query = "SET TRANSACTION READ ONLY; " + configured_timeout_sql("statement_timeout", "preflight_timeout_seconds") + "; " + query
        with self.engine.connect() as conn:
            issues = conn.execute(text(query), {
                "schema": self.schema, "recipe": recipe, "retry": False,
                "required": json.dumps({key: sorted(columns) for key, columns in required.items()}),
                "keys": json.dumps(keys), "foreign_keys": json.dumps(foreign_keys),
            }).scalar_one()
        if issues:
            raise RuntimeError("; ".join(issues))

    def candidate_batch(self, *, recipe, limit=PREVIEW_BATCH_SIZE, after_md5=None, only_md5s=(), retry_known_failures=False):
        boolean(retry_known_failures, "retry_known_failures")
        integer(limit, "limit")
        if limit > PREVIEW_BATCH_SIZE:
            raise ValueError("Preview batches must not exceed 200 documents")
        parameters = {"recipe": nonblank(recipe, "recipe"), "retry": retry_known_failures, "limit": limit}
        query = SOURCES + PENDING
        if after_md5 is not None:
            parameters["after_md5"] = document_md5(after_md5)
            query += " AND d.md5>:after_md5"
        if only_md5s:
            parameters["md5s"] = [document_md5(md5) for md5 in only_md5s]
            query += " AND d.md5=ANY(:md5s)"
        query += " ORDER BY d.md5 LIMIT :limit"
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(text(query), parameters).mappings()]

    def _command(self, action, expected, *, recipe, operation=None, **values):
        payload = {
            "action": action, "md5": document_md5(expected["md5"]),
            "recipe": nonblank(recipe, "recipe"),
            "snapshot": {field: expected[field] for field in ("md5", *SNAPSHOT_FIELDS)},
            **values,
        }
        source_query = SOURCES.replace(":recipe", "recipe_key") + " AND d.md5=document_key"
        pending_query = (SOURCES + PENDING).replace(":recipe", "recipe_key").replace(":retry", "retry")
        pending_query += " AND d.md5=document_key"
        return preview_command(self.engine, self.schema, payload, source_query=source_query,
                               pending_query=pending_query, operation=operation)

    def claim(self, expected, *, recipe, actor, run_id, retry_known_failures, lease_seconds, operation):
        boolean(retry_known_failures, "retry_known_failures")
        integer(run_id, "run_id")
        self._lease(lease_seconds)
        return self._command("claim", expected, recipe=recipe, operation=operation,
                             actor=nonblank(actor, "actor"), retry=retry_known_failures,
                             idempotency_key=f"recipe:{recipe}:run:{run_id}",
                             claim_token=uuid.uuid4().hex, lease_seconds=lease_seconds)

    @staticmethod
    def _lease(seconds):
        integer(seconds, "lease_seconds")
        if seconds > 3600:
            raise ValueError("preview lease must not exceed one hour")

    def renew(self, expected, request, *, lease_seconds, operation):
        self._lease(lease_seconds)
        return self._command("renew", expected, recipe=request["recipe"], operation=operation,
                             request_id=integer(request["request_id"], "request_id"),
                             claim_token=nonblank(request["claim_token"], "claim_token"),
                             lease_seconds=lease_seconds)

    def finish(self, expected, request, *, pages, source_page_count, actor, operation=None, error=None):
        validate_pages(pages, source_page_count)
        return self._command("fail" if error is not None else "finish", expected,
                             recipe=request["recipe"], operation=operation,
                             request_id=integer(request["request_id"], "request_id"),
                             claim_token=nonblank(request["claim_token"], "claim_token"),
                             pages=pages, source_page_count=source_page_count, actor=nonblank(actor, "actor"))
