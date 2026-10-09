"""Catalog-native file discovery and deletion; publication identity is retained."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

from sqlalchemy import inspect, text

from app.catalog.contracts import CatalogConflict, boolean, document_md5, integer
from app.catalog.document_sync_bulk import insert_records, reserve_publications, update_records
from app.document_cleanup_paths import source_path
from app.document_storage import normalized_extension
from app.document_operation_lock import DocumentOperationBusy, lock_document_transaction


_DOCUMENTS = """
    SELECT d.md5, d.publication_id, d.revision AS document_revision,
           d.mime_type, d.complete AS "full", d.restricted AS sharing_restricted,
           y.source_path AS ya_path, y.resource_id AS ya_resource_id,
           y.public_url AS ya_public_url, y.public_key AS ya_public_key,
           y.revision AS source_revision, y.size AS source_size,
           s.locator AS document_url, s.size AS primary_storage_size,
           s.etag AS primary_storage_etag, s.verified_at AS primary_storage_verified_at,
           state.verified_source_mime
    FROM catalog_documents d
    LEFT JOIN catalog_locations y
      ON y.md5=d.md5 AND y.provider='yandex' AND y.purpose='source'
    LEFT JOIN catalog_locations s
      ON s.md5=d.md5 AND s.provider='s3' AND s.purpose='primary'
    LEFT JOIN library_non_pdf_extraction_state state ON state.md5=d.md5
"""
_SNAPSHOT_FIELDS = ("publication_id", "document_revision", "source_revision")
SYNC_BATCH_SIZE = 250


def _json(value: Any) -> str:
    return json.dumps(value, default=lambda item: item.isoformat(), ensure_ascii=False)


def discovery_values(payload):
    """Validate provider facts before buffering or applying a catalog command."""
    md5 = document_md5(payload['md5'])
    values = {'mime_type': payload['mime_type'], 'complete': boolean(payload['full'], 'full'),
              'restricted': boolean(payload['sharing_restricted'], 'sharing_restricted')}
    if not isinstance(values['mime_type'], str) or not values['mime_type'].strip():
        raise ValueError('mime_type must be nonblank text')
    for field in ('ya_resource_id', 'ya_public_url', 'ya_public_key'):
        if payload.get(field) is not None and not isinstance(payload[field], str):
            raise ValueError(f'{field} must be text or null')
    size = payload.get('source_size')
    if size is not None:
        integer(size, 'source_size', minimum=0)
        if size > 2**63 - 1:
            raise ValueError('source_size exceeds PostgreSQL bigint capacity')
    source = {'source_path': source_path(payload['ya_path']), 'resource_id': payload.get('ya_resource_id'),
              'size': size, 'public_url': payload.get('ya_public_url'), 'public_key': payload.get('ya_public_key')}
    return md5, values, source


def _check_expected(md5, current, expected):
    if (current is None) != (expected is None) or (current and any(
            current[key] != expected[key] for key in _SNAPSHOT_FIELDS)):
        raise CatalogConflict(f'Document {md5} changed during sync; rerun Sync')


def _queue_update(kind, before, values, updates):
    if before and any(before[key] != value for key, value in values.items()):
        updates[kind].append({**before, **values})
        return True
    return False


class DocumentSyncStore:
    def __init__(self, engine, *, schema: str):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError("Invalid catalog schema")
        self.engine = engine
        self.schema = schema

    def preflight(self) -> None:
        """Inspect the deployed schema before any storage or catalog mutation."""
        required = {
            "catalog_publications": {"publication_id", "inclusion", "has_metadata", "metadata_present", "revision"},
            "catalog_documents": {"md5", "publication_id", "mime_type", "complete", "restricted", "selected", "revision", "updated_at"},
            "catalog_locations": {"location_id", "md5", "provider", "purpose", "source_path", "resource_id", "public_url", "public_key", "locator", "size", "etag", "verified_at", "revision", "updated_at"},
            "catalog_protections": {"record_kind", "record_key", "field"},
            "catalog_revisions": {"record_kind", "record_key", "actor", "before", "after"},
            "library_non_pdf_extraction_state": {"md5", "verified_source_mime"},
            "library_upstream_metadata": {"md5"},
            "library_isbn_duplicate_reviews": {"review_id", "status", "candidates_json", "evidence_json", "keep_md5s_json"},
            "document_cleanup_queue": {"cleanup_id", "scope", "action", "reason", "md5", "source_resource_id", "source_path", "target_path", "status", "phase", "evidence_json", "updated_at", "completed_at"},
        }
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SET LOCAL statement_timeout = '5s'"))
            inspector = inspect(conn)
            for relation, columns in required.items():
                if not inspector.has_table(relation, schema=self.schema):
                    raise RuntimeError(f"Sync requires catalog relation {relation}; apply migrations separately")
                present = {item['name'] for item in inspector.get_columns(relation, schema=self.schema)}
                if columns - present:
                    raise RuntimeError(f"Sync catalog is missing {relation} columns: {sorted(columns - present)}")
            version_schema = os.environ.get("MANZARA_ALEMBIC_VERSION_SCHEMA", self.schema)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", version_schema):
                raise ValueError("Invalid migration version schema")
            revision = str(conn.execute(text(f'SELECT version_num FROM "{version_schema}".alembic_version_manzara')).scalar_one())
            if not re.fullmatch(r"\d{8}_\d{4}", revision) or int(revision.split('_')[1]) < 62:
                raise RuntimeError("Sync requires catalog revision 20261008_0062 or later")
            pk = inspector.get_pk_constraint("catalog_documents", schema=self.schema)
            if pk['constrained_columns'] != ['md5']:
                raise RuntimeError("Sync requires an unambiguous document MD5 primary key")
            unique = inspector.get_unique_constraints("catalog_locations", schema=self.schema)
            if not any(set(item['column_names']) == {'md5', 'provider', 'purpose'} for item in unique):
                raise RuntimeError("Sync requires unique document/provider/purpose locations")
            # File-owned facts must disappear with the file, never with its publication.
            for relation in ("catalog_locations", "catalog_document_access_modes", "catalog_sufficient_modes",
                             "catalog_preview_requests", "library_non_pdf_extraction_state"):
                foreign_keys = inspector.get_foreign_keys(relation, schema=self.schema)
                if not any(item['referred_table'] == 'catalog_documents'
                           and item['constrained_columns'] == ['md5']
                           and item['options'].get('ondelete') == 'CASCADE' for item in foreign_keys):
                    raise RuntimeError(f"Sync requires file-owned cascade for {relation}")

    def list_documents(self) -> dict[str, dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(text(_DOCUMENTS + " ORDER BY d.md5")).mappings()
            result = {}
            for row in rows:
                md5 = document_md5(row['md5'])
                if md5 in result:
                    raise RuntimeError(f"Duplicate document MD5 {md5}; refusing sync")
                result[md5] = dict(row)
            return result

    def _snapshot(self, conn, md5):
        row = conn.execute(text(_DOCUMENTS + " WHERE d.md5=:md5"), {"md5": md5}).mappings().one_or_none()
        return dict(row) if row else None

    def _lock(self, conn, md5):
        lock_document_transaction(conn, md5)
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext(:identity))"),
                     {"identity": f"catalog-document:{md5}"})
        row = conn.execute(text("SELECT * FROM catalog_documents WHERE md5=:md5 FOR UPDATE"),
                           {"md5": md5}).mappings().one_or_none()
        conn.execute(text("SELECT location_id FROM catalog_locations WHERE md5=:md5 ORDER BY location_id FOR UPDATE"),
                     {"md5": md5}).all()
        return dict(row) if row else None

    def _audit(self, conn, kind, key, before, after):
        conn.execute(text('''INSERT INTO catalog_revisions(record_kind,record_key,actor,"before","after")
            VALUES(:kind,:key,'task',CAST(:before AS JSONB),CAST(:after AS JSONB))'''),
            {"kind": kind, "key": str(key), "before": _json(before), "after": _json(after)})

    def _lock_batch(self, conn, md5s):
        conn.execute(text('''SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext(identity))
            FROM (SELECT 'catalog-document:' || md5 AS identity
                  FROM unnest(CAST(:md5s AS TEXT[])) AS item(md5) ORDER BY md5) AS identities'''),
            {'md5s': md5s}).all()
        documents = {row['md5']: dict(row) for row in conn.execute(text('''
            SELECT * FROM catalog_documents WHERE md5=ANY(:md5s) ORDER BY md5 FOR UPDATE'''),
            {'md5s': md5s}).mappings()}
        locations = [dict(row) for row in conn.execute(text('''
            SELECT * FROM catalog_locations WHERE md5=ANY(:md5s) ORDER BY location_id FOR UPDATE'''),
            {'md5s': md5s}).mappings()]
        return documents, locations

    def sync_documents(self, conn, commands):
        """Revalidate and apply one bounded batch with set-based writes and audits."""
        if len(commands) > SYNC_BATCH_SIZE:
            raise ValueError(f'Sync batch must contain at most {SYNC_BATCH_SIZE} documents')
        prepared = {}
        for item in commands:
            md5, values, source = discovery_values(item['payload'])
            prepared[md5] = (values, source, item['expected'])
        if len(prepared) != len(commands):
            raise ValueError('Sync batch contains repeated MD5 identities')
        if not prepared:
            return {'results': {}, 'conflicts': {}}
        conn.execute(text("SELECT set_config('lock_timeout','5s',true), "
                          "set_config('statement_timeout','30s',true)"))
        conflicts = {}
        for md5 in sorted(prepared):
            try:
                lock_document_transaction(conn, md5)
            except DocumentOperationBusy as exc:
                conflicts[md5] = str(exc)
        md5s = sorted(set(prepared) - conflicts.keys())
        if not md5s:
            return {'results': {}, 'conflicts': conflicts}
        documents, locations = self._lock_batch(conn, md5s)
        snapshots = {row['md5']: dict(row) for row in conn.execute(
            text(_DOCUMENTS + ' WHERE d.md5=ANY(:md5s)'), {'md5s': md5s}).mappings()}
        protected = {}
        for row in conn.execute(text('''SELECT record_key,field FROM catalog_protections
            WHERE record_kind='document' AND record_key=ANY(:md5s)'''), {'md5s': md5s}).mappings():
            protected.setdefault(row['record_key'], set()).add(row['field'])
        paths = [source['source_path'] for _, source, _ in prepared.values()]
        active = list(conn.execute(text('''SELECT md5,scope,source_path FROM document_cleanup_queue
            WHERE status IN ('planned','running','failed') AND
                  ((scope='document' AND md5=ANY(:md5s)) OR source_path=ANY(:paths))'''),
            {'md5s': md5s, 'paths': paths}).mappings())
        excluded_md5s = {row['md5'] for row in active if row['scope'] == 'document'}
        excluded_paths = {row['source_path'] for row in active}
        sources = {row['md5']: row for row in locations if (row['provider'], row['purpose']) == ('yandex', 'source')}
        primaries = {row['md5']: row for row in locations if (row['provider'], row['purpose']) == ('s3', 'primary')}
        updates = {kind: [] for kind in ('document', 'source', 'primary')}
        new_documents, new_sources, outcomes = [], [], {}
        for md5 in md5s:
            values, source, expected = prepared[md5]
            before, current = documents.get(md5), snapshots.get(md5)
            try:
                _check_expected(md5, current, expected)
                if md5 in excluded_md5s or source['source_path'] in excluded_paths:
                    raise CatalogConflict(f'Document {md5} acquired an active cleanup plan; rerun Sync')
                if before:
                    values['restricted'] = values['restricted'] or before['restricted'] is not False
                    if current['verified_source_mime']:
                        values['mime_type'] = current['verified_source_mime']
                    fields = {key for key, value in values.items() if before[key] != value}
                    blocked = fields & protected.get(md5, set())
                    if blocked:
                        raise CatalogConflict(f'Document {md5} has protected changes {sorted(blocked)}; review before retrying')
            except CatalogConflict as exc:
                conflicts[md5] = str(exc)
                continue
            if values['restricted']:
                source.update(public_url=None, public_key=None)
            changed = _queue_update('document', before, values, updates)
            if before is None:
                new_documents.append({'md5': md5, **values})
            existing_source = sources.get(md5)
            if existing_source:
                changed = _queue_update('source', existing_source, source, updates) or changed
            else:
                new_sources.append({'md5': md5, 'provider': 'yandex', 'purpose': 'source', **source})
                changed = True
            if current and (current['mime_type'] != values['mime_type']
                            or current['sharing_restricted'] != values['restricted']
                            or normalized_extension(current['ya_path'] or '', current['mime_type'])
                            != normalized_extension(source['source_path'], values['mime_type'])):
                _queue_update('primary', primaries.get(md5),
                              {'locator': None, 'size': None, 'etag': None, 'verified_at': None}, updates)
            outcomes[md5] = 'created' if before is None else 'updated' if changed else 'unchanged'
        identities = reserve_publications(conn, [row['md5'] for row in new_documents])
        insert_records(conn, 'publication', [{'publication_id': identity, 'has_metadata': False,
                                             'metadata_present': False, 'inclusion': 'pending'}
                                            for identity in identities.values()])
        insert_records(conn, 'document', [{**row, 'publication_id': identities[row['md5']]}
                                         for row in new_documents])
        update_records(conn, 'document', updates['document'], documents)
        insert_records(conn, 'source', new_sources)
        before_locations = {row['location_id']: row for row in locations}
        for kind in ('source', 'primary'):
            update_records(conn, kind, updates[kind], before_locations)
        after = {row['md5']: dict(row) for row in conn.execute(
            text(_DOCUMENTS + ' WHERE d.md5=ANY(:md5s)'), {'md5s': list(outcomes)}).mappings()}
        return {'results': {md5: {'outcome': outcome, 'document': after[md5]}
                            for md5, outcome in outcomes.items()}, 'conflicts': conflicts}

    def validate_publication(self, conn, expected):
        md5 = document_md5(expected['md5'])
        current = self._snapshot(conn, md5)
        _check_expected(md5, current, expected)
        if current['sharing_restricted'] is not False:
            raise CatalogConflict(f'Document {md5} became restricted; publication refused')

    def delete_document(self, conn, md5: str, *, expected: Mapping[str, Any] | None):
        """Delete file-owned facts, retaining publication metadata and all history."""
        document_md5(md5)
        before = self._lock(conn, md5)
        current = self._snapshot(conn, md5)
        if current and (expected is None or any(current[key] != expected[key] for key in _SNAPSHOT_FIELDS)):
            raise CatalogConflict(f"Document {md5} changed before cleanup; inspect the plan")
        conn.execute(text("DELETE FROM library_upstream_metadata WHERE md5=:md5"), {"md5": md5})
        if not before:
            return
        locations = conn.execute(text("SELECT * FROM catalog_locations WHERE md5=:md5"), {"md5": md5}).mappings().all()
        for location in locations:
            self._audit(conn, 'location', location['location_id'], dict(location), None)
            conn.execute(text("DELETE FROM catalog_protections WHERE record_kind='location' AND record_key=:key"),
                         {"key": str(location['location_id'])})
        self._audit(conn, 'document', md5, before, None)
        conn.execute(text("DELETE FROM catalog_protections WHERE record_kind='document' AND record_key=:md5"), {"md5": md5})
        conn.execute(text("DELETE FROM catalog_documents WHERE md5=:md5"), {"md5": md5})
