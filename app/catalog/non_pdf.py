"""Catalog-native non-PDF source snapshots and transactional result writes."""

from __future__ import annotations

import re
from contextlib import contextmanager, nullcontext

from sqlalchemy import inspect, text

from app.catalog.contracts import CatalogConflict
from app.catalog.repository import CatalogRepository
from app.document_operation_lock import lock_document_transaction
from app.postgres_engine import configured_timeout_sql
from app.settings import configured_schema

SOURCES = """
    SELECT d.md5, d.mime_type, d.revision AS document_revision,
           y.source_path AS ya_path, y.revision AS source_revision,
           s.locator AS document_url, s.size AS primary_storage_size,
           s.revision AS primary_revision,
           c.locator AS content_url, c.revision AS content_revision,
           state.extractor_version, state.detected_format,
           state.status AS durable_status, state.updated_at AS durable_updated_at
    FROM catalog_documents d
    JOIN catalog_locations s ON s.md5=d.md5 AND s.provider='s3' AND s.purpose='primary'
    LEFT JOIN catalog_locations y ON y.md5=d.md5 AND y.provider='yandex' AND y.purpose='source'
    LEFT JOIN catalog_locations c ON c.md5=d.md5 AND c.provider='s3' AND c.purpose='content'
    LEFT JOIN library_non_pdf_extraction_state state ON state.md5=d.md5
    WHERE d.restricted IS FALSE
      AND NULLIF(BTRIM(s.locator),'') IS NOT NULL
      AND s.size IS NOT NULL AND s.verified_at IS NOT NULL
      AND LOWER(BTRIM(COALESCE(d.mime_type,''))) <> 'application/pdf'
      AND NOT EXISTS (
          SELECT 1 FROM document_cleanup_queue q WHERE q.md5=d.md5
            AND q.scope='document' AND q.status IN ('planned','running','failed'))
"""
SNAPSHOT_FIELDS = (
    "document_revision", "source_revision", "primary_revision", "content_revision",
    "document_url", "primary_storage_size", "content_url",
)


class NonPdfCatalogStore:
    def __init__(self, engine, *, schema: str):
        self.engine = engine
        self.schema = schema
        self.catalog = CatalogRepository(engine, schema=schema)

    def preflight(self) -> None:
        """Read the deployed contract before any conversion or publication."""
        required = {
            "catalog_documents": {"md5", "publication_id", "mime_type", "restricted", "revision", "updated_at", "content_extraction_method"},
            "catalog_locations": {"location_id", "md5", "provider", "purpose", "source_path", "locator", "size", "etag", "verified_at", "revision", "updated_at"},
            "catalog_protections": {"record_kind", "record_key", "field"},
            "catalog_revisions": {"record_kind", "record_key", "actor", "before", "after"},
            "library_non_pdf_extraction_state": {"md5", "extractor_version", "detected_format", "status", "generated_at", "verified_source_mime", "decision_reason", "created_at", "updated_at"},
            "document_cleanup_queue": {"cleanup_id", "scope", "action", "reason", "md5", "source_resource_id", "source_path", "target_path", "status", "evidence_json"},
        }
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text(configured_timeout_sql("statement_timeout", "preflight_timeout_seconds")))
            inspector = inspect(conn)
            tables = set(inspector.get_table_names(schema=self.schema))
            for relation, columns in required.items():
                if relation not in tables:
                    raise RuntimeError(f"Non-PDF extraction requires catalog table {relation}; apply migrations separately")
                present = {column['name'] for column in inspector.get_columns(relation, schema=self.schema)}
                if columns - present:
                    raise RuntimeError(f"Non-PDF catalog is missing {relation} columns: {sorted(columns - present)}")
            version_schema = configured_schema("migration_version_schema")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", version_schema):
                raise ValueError("Invalid migration version schema")
            revision = str(conn.execute(text(f'SELECT version_num FROM "{version_schema}".alembic_version_manzara')).scalar_one())
            if not re.fullmatch(r"\d{8}_\d{4}", revision) or int(revision.split('_')[1]) < 62:
                raise RuntimeError("Non-PDF extraction requires catalog revision 20261008_0062 or later")
            for relation in ("catalog_documents", "library_non_pdf_extraction_state"):
                if inspector.get_pk_constraint(relation, schema=self.schema)['constrained_columns'] != ['md5']:
                    raise RuntimeError(f"Non-PDF extraction requires the {relation} MD5 primary key")
            unique = inspector.get_unique_constraints("catalog_locations", schema=self.schema)
            if not any(set(item['column_names']) == {'md5', 'provider', 'purpose'} for item in unique):
                raise RuntimeError("Non-PDF extraction requires unique document/provider/purpose locations")
            for relation in ("catalog_locations", "library_non_pdf_extraction_state"):
                foreign_keys = inspector.get_foreign_keys(relation, schema=self.schema)
                if not any(item['referred_table'] == 'catalog_documents'
                           and item['constrained_columns'] == ['md5']
                           and item['options'].get('ondelete') == 'CASCADE' for item in foreign_keys):
                    raise RuntimeError(f"Non-PDF extraction requires file-owned cascade for {relation}")
            # Validate the actual read envelope, including joined location purposes.
            conn.execute(text(SOURCES + " ORDER BY d.md5 LIMIT 1")).mappings().all()

    def list_sources(self, *, only_md5s=None, checkpoint_version=None):
        clause, parameters = "", {}
        if only_md5s is not None:
            clause += " AND d.md5=ANY(:md5s)"
            parameters['md5s'] = sorted(only_md5s)
        if checkpoint_version is not None:
            clause += " AND state.extractor_version=:version AND state.detected_format='powerpoint'"
            parameters['version'] = checkpoint_version
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(
                text(SOURCES + clause + " ORDER BY d.md5"), parameters,
            ).mappings()]

    def _source(self, conn, md5):
        row = conn.execute(text(SOURCES + " AND d.md5=:md5"), {"md5": md5}).mappings().one_or_none()
        return dict(row) if row else None

    def _lock(self, conn, md5):
        lock_document_transaction(conn, md5)
        # Same order and document lock identity as catalog sync/cleanup.
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext(:identity))"),
                     {"identity": f"catalog-document:{md5}"})
        row = conn.execute(text("SELECT * FROM catalog_documents WHERE md5=:md5 FOR UPDATE"),
                           {"md5": md5}).mappings().one_or_none()
        conn.execute(text("SELECT location_id FROM catalog_locations WHERE md5=:md5 ORDER BY location_id FOR UPDATE"),
                     {"md5": md5}).all()
        return dict(row) if row else None

    def _check(self, conn, expected):
        current = self._source(conn, expected['md5'])
        if current is None or any(current[key] != expected[key] for key in SNAPSHOT_FIELDS):
            raise CatalogConflict(f"Document {expected['md5']} changed or is no longer eligible; resume with a fresh snapshot")
        return current

    @contextmanager
    def publication(self, expected, *, conn=None):
        """Keep file/privacy and location edits out of the publication boundary."""
        with nullcontext(conn) if conn is not None else self.engine.begin() as connection:
            if conn is None:
                connection.execute(text("SET TRANSACTION READ WRITE"))
                connection.execute(text(configured_timeout_sql("lock_timeout", "lock_timeout_seconds")))
            self._lock(connection, expected['md5'])
            self._check(connection, expected)
            yield connection

    def record_verified_mime(self, expected, *, detected_format, version, mime, previous_version=None, conn=None):
        if {"doc": "application/msword", "powerpoint": "application/vnd.ms-powerpoint"}.get(detected_format) != mime:
            raise ValueError("MIME corrections require verified DOC or PowerPoint root streams")
        with self.publication(expected, conn=conn) as conn:
            before = self.catalog._record(conn, "document", expected['md5'])
            self._patch_document(conn, before, {"mime_type": mime})
            if previous_version is not None:
                updated = conn.execute(text("""UPDATE library_non_pdf_extraction_state
                    SET detected_format=:format, verified_source_mime=:mime, updated_at=CURRENT_TIMESTAMP
                    WHERE md5=:md5 AND extractor_version=:version AND detected_format='powerpoint'"""),
                    {"md5": expected['md5'], "format": detected_format, "mime": mime, "version": previous_version})
                if updated.rowcount != 1:
                    raise CatalogConflict(f"OLE checkpoint changed for {expected['md5']}")
            else:
                # MIME facts must not replace a retained successful recipe/result.
                conn.execute(text("""INSERT INTO library_non_pdf_extraction_state
                    (md5,extractor_version,detected_format,status,verified_source_mime)
                    VALUES(:md5,:version,:format,'detected',:mime)
                    ON CONFLICT(md5) DO UPDATE SET verified_source_mime=excluded.verified_source_mime"""),
                    {"md5": expected['md5'], "version": version, "format": detected_format, "mime": mime})
            return self._source(conn, expected['md5'])

    def _patch_document(self, conn, before, changes, *, touch=False):
        changes = {key: value for key, value in changes.items() if before[key] != value}
        blocked = set(changes) & self.catalog._protected(conn, "document", before['md5'])
        if blocked:
            raise CatalogConflict(f"Protected document fields changed for {before['md5']}: {sorted(blocked)}")
        if changes or touch:
            self.catalog._update(conn, "document", before['md5'], before, changes, "task")

    def save_success(self, conn, expected, *, version, detected_format, content_url, size, etag):
        self._check(conn, expected)
        before = self.catalog._record(conn, "document", expected['md5'])
        # Every new generation changes the document's optimistic revision, even
        # when its formatter recipe is unchanged and only its location changes.
        self._patch_document(conn, before, {"content_extraction_method": version}, touch=True)
        old_location = conn.execute(text("""SELECT * FROM catalog_locations
            WHERE md5=:md5 AND provider='s3' AND purpose='content'"""), {'md5': expected['md5']}).mappings().one_or_none()
        after = dict(conn.execute(text("""INSERT INTO catalog_locations
            (md5,provider,purpose,locator,size,etag,verified_at)
            VALUES(:md5,'s3','content',:url,:size,:etag,CURRENT_TIMESTAMP)
            ON CONFLICT(md5,provider,purpose) DO UPDATE SET locator=excluded.locator,
                size=excluded.size,etag=excluded.etag,verified_at=excluded.verified_at,
                revision=catalog_locations.revision+1,updated_at=CURRENT_TIMESTAMP
            RETURNING *"""), {'md5': expected['md5'], 'url': content_url, 'size': size, 'etag': etag}).mappings().one())
        self.catalog._audit(conn, "location", after['location_id'], dict(old_location) if old_location else None, after, "task")
        conn.execute(text("""INSERT INTO library_non_pdf_extraction_state
            (md5,extractor_version,detected_format,status,generated_at)
            VALUES(:md5,:version,:format,'ready',CURRENT_TIMESTAMP)
            ON CONFLICT(md5) DO UPDATE SET extractor_version=excluded.extractor_version,
                detected_format=excluded.detected_format,status='ready',decision_reason=NULL,
                updated_at=CURRENT_TIMESTAMP,generated_at=CURRENT_TIMESTAMP"""),
            {'md5': expected['md5'], 'version': version, 'format': detected_format})

    def save_unsupported(self, expected, *, version, detected_format, reason):
        with self.publication(expected) as conn:
            conn.execute(text("""INSERT INTO library_non_pdf_extraction_state
                (md5,extractor_version,detected_format,status,decision_reason)
                VALUES(:md5,:version,:format,'unsupported',:reason)
                ON CONFLICT(md5) DO UPDATE SET extractor_version=excluded.extractor_version,
                    detected_format=excluded.detected_format,status='unsupported',decision_reason=excluded.decision_reason,
                    updated_at=CURRENT_TIMESTAMP"""),
                {'md5': expected['md5'], 'version': version, 'format': detected_format, 'reason': reason})
