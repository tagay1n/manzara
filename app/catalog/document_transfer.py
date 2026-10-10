"""Catalog-native transfer snapshots and verified primary-location commands."""

from sqlalchemy import text

from app.catalog.contracts import CatalogConflict, document_md5, integer
from app.catalog.document_sync import DocumentSyncStore
from app.catalog.document_sync_bulk import update_records
from app.document_cleanup_paths import source_path
from app.document_operation_lock import check_document_operation
from app.postgres_engine import configured_timeout_sql
from app.runtime_config import config_integer

_SNAPSHOT = """
    SELECT d.md5, d.publication_id, d.revision AS document_revision,
           d.mime_type, d.restricted AS sharing_restricted,
           y.location_id AS source_id, y.revision AS source_revision,
           y.source_path AS ya_path, y.resource_id AS ya_resource_id, y.size AS source_size,
           s.location_id AS primary_id, s.revision AS primary_revision,
           s.locator AS document_url, s.size AS primary_storage_size,
           s.etag AS primary_storage_etag, s.verified_at AS primary_storage_verified_at
    FROM catalog_documents d
    LEFT JOIN catalog_locations y ON y.md5=d.md5 AND y.provider='yandex' AND y.purpose='source'
    LEFT JOIN catalog_locations s ON s.md5=d.md5 AND s.provider='s3' AND s.purpose='primary'
"""
_PENDING = """(NULLIF(BTRIM(s.locator),'') IS NULL OR s.size IS NULL
    OR NULLIF(BTRIM(s.etag),'') IS NULL OR s.verified_at IS NULL)"""
_AVAILABLE = """NOT EXISTS (
    SELECT 1 FROM document_cleanup_queue q WHERE q.status IN ('planned','running','failed')
    AND ((q.scope='document' AND q.md5=d.md5)
         OR regexp_replace(q.source_path, '^disk:', '')=regexp_replace(y.source_path, '^disk:', '')))
"""
_IDENTITY = ("md5", "publication_id", "document_revision", "mime_type", "sharing_restricted",
             "source_id", "source_revision", "ya_path", "ya_resource_id", "source_size",
             "primary_id", "primary_revision", "document_url", "primary_storage_size",
             "primary_storage_etag", "primary_storage_verified_at")


def pending(row):
    return (not str(row.get('document_url') or '').strip()
            or row.get('primary_storage_size') is None
            or not str(row.get('primary_storage_etag') or '').strip()
            or row.get('primary_storage_verified_at') is None)


class DocumentTransferStore:
    def __init__(self, engine, *, schema):
        self.engine = engine
        self.catalog = DocumentSyncStore(engine, schema=schema)

    def preflight(self):
        self.catalog.preflight()
        if self.engine.pool.size() < 2:
            raise ValueError('Transfer requires database_pool_size >= 2')

    def list_pending_documents(self, *, after=''):
        with self.engine.connect() as conn:
            rows = conn.execute(text(_SNAPSHOT + f" WHERE {_PENDING} AND {_AVAILABLE} "
                                     "AND d.md5>:after ORDER BY d.md5 LIMIT :limit"),
                                {'after': after, 'limit': config_integer("maintenance", "transfer_batch_size")}).mappings()
            return [dict(row) for row in rows]

    def count_pending_documents(self):
        with self.engine.connect() as conn:
            return int(conn.execute(text("SELECT COUNT(*) FROM (" + _SNAPSHOT
                                        + f" WHERE {_PENDING} AND {_AVAILABLE}) AS candidates")).scalar_one())

    def snapshot(self, conn, md5):
        row = conn.execute(text(_SNAPSHOT + ' WHERE d.md5=:md5'), {'md5': md5}).mappings().one_or_none()
        return dict(row) if row else None

    def validate(self, conn, expected, *, payload=None):
        current = self.snapshot(conn, document_md5(expected['md5']))
        if current is None or any(current[key] != expected[key] for key in _IDENTITY):
            raise CatalogConflict(f"Document {expected['md5']} changed; retry from its current snapshot")
        if type(current['sharing_restricted']) is not bool:
            raise CatalogConflict('Document privacy is unknown; review before transfer')
        if not pending(current):
            raise CatalogConflict('Document already has a complete primary checkpoint')
        if current['ya_path'] is not None:
            source_path(current['ya_path'])
        for field in ('source_size', 'primary_storage_size'):
            if current[field] is not None:
                integer(current[field], field, minimum=0)
        active = conn.execute(text("SELECT EXISTS (" + _SNAPSHOT
                                   + f" WHERE d.md5=:md5 AND NOT ({_AVAILABLE}))"),
                              {'md5': current['md5']}).scalar_one()
        if active:
            raise CatalogConflict('Active cleanup owns this document/source; retry after review')
        protected = set(conn.execute(text("""SELECT field FROM catalog_protections
            WHERE (record_kind='document' AND record_key=:md5)
               OR (record_kind='location' AND record_key=CAST(:primary AS TEXT))"""),
            {'md5': current['md5'], 'primary': current['primary_id']}).scalars())
        fields = {'locator': 'document_url', 'size': 'primary_storage_size',
                  'etag': 'primary_storage_etag', 'verified_at': 'primary_storage_verified_at'}
        # Honoring a protected privacy/MIME fact does not modify that fact.
        # Existing protected locators can still have unprotected evidence repaired.
        changes = {key for key, legacy in fields.items()
                   if (payload is not None and payload[key] != current[legacy])
                   or (payload is None and (current[legacy] is None
                       or (key in {'locator', 'etag'} and not str(current[legacy]).strip())
                       or key == 'verified_at'))}
        blocked = {key for key in changes if key in protected or fields[key] in protected}
        if blocked:
            raise CatalogConflict(f'Protected transfer fields require review: {sorted(blocked)}')
        return current

    def revalidate(self, conn, expected):
        check_document_operation(conn)
        with conn.begin():
            return self.validate(conn, expected)

    def save_storage_checkpoint(self, conn, expected, payload):
        check_document_operation(conn)
        with conn.begin():
            conn.execute(text('SET TRANSACTION READ WRITE'))
            conn.execute(text(configured_timeout_sql("lock_timeout", "lock_timeout_seconds")))
            self.catalog._lock(conn, expected['md5'])
            self.validate(conn, expected, payload=payload)
            before = conn.execute(text("""SELECT * FROM catalog_locations
                WHERE md5=:md5 AND provider='s3' AND purpose='primary'"""), expected).mappings().one_or_none()
            values = {key: payload[key] for key in ('locator', 'size', 'etag', 'verified_at')}
            if before:
                update_records(conn, 'primary', [{**before, **values}], {before['location_id']: dict(before)})
            else:
                after = dict(conn.execute(text("""INSERT INTO catalog_locations
                    (md5,provider,purpose,locator,size,etag,verified_at)
                    VALUES (:md5,'s3','primary',:locator,:size,:etag,:verified_at) RETURNING *"""),
                    {'md5': expected['md5'], **values}).mappings().one())
                self.catalog._audit(conn, 'location', after['location_id'], None, after)
