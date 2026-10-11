
from app.postgres_engine import configured_timeout_sql
from app.runtime_config import config_integer
from app.settings import configured_schema

"""Publication metadata snapshots and audited, catalog-native task commands."""

import hashlib
import json
import re
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from sqlalchemy import inspect, select, text

from app.catalog.contracts import CatalogConflict, integer
from app.catalog.metadata import compose_metadata
from app.catalog.metadata_save import save_metadata_result
from app.catalog.metadata_store import unmanaged_subjects
from app.catalog.repository import CatalogRepository

SOURCE_FROM = """
 FROM catalog_documents d
 JOIN catalog_locations s ON s.md5=d.md5 AND s.provider='s3' AND s.purpose='primary'
 LEFT JOIN catalog_locations c ON c.md5=d.md5 AND c.provider='s3' AND c.purpose='content'
"""
SOURCE_WHERE = """
 WHERE d.complete IS TRUE AND d.restricted IS FALSE
   AND NULLIF(BTRIM(s.locator),'') IS NOT NULL
   AND s.size IS NOT NULL AND s.verified_at IS NOT NULL
   AND (NULLIF(BTRIM(c.locator),'') IS NOT NULL
        OR LOWER(BTRIM(d.mime_type)) IN ('application/pdf','image/vnd.djvu'))
   AND NOT EXISTS (SELECT 1 FROM document_cleanup_queue q
       WHERE q.md5=d.md5 AND q.scope='document' AND q.status IN ('planned','running','failed'))
"""
SOURCES = """
 SELECT d.*, s.locator AS document_url, s.size AS primary_storage_size,
        s.revision AS primary_revision, y.source_path,
        y.revision AS source_revision, c.locator AS content_url,
        c.revision AS content_revision, u.payload_json AS upstream_metadata
""" + SOURCE_FROM + """
 LEFT JOIN catalog_locations y ON y.md5=d.md5 AND y.provider='yandex' AND y.purpose='source'
 LEFT JOIN library_upstream_metadata u ON u.md5=d.md5
""" + SOURCE_WHERE
METADATA_TASK_ACTORS = ('library.metadata_extract', 'maintenance.monocorpus_meta_evaluate')
PUBLICATION_BATCH = """
 SELECT p.* FROM catalog_publications p
 WHERE p.merged_into_id IS NULL AND ({{eligibility}})
   AND p.publication_id > :after_id
   AND EXISTS (SELECT 1
""" + SOURCE_FROM + SOURCE_WHERE + """
       AND d.publication_id=p.publication_id)
   AND NOT EXISTS (
       SELECT 1 FROM catalog_proposals q WHERE q.publication_id=p.publication_id
       AND q.kind='metadata' AND q.status IN ('pending','deferred')
       AND q.evidence->>'actor'=ANY(:actors))
 ORDER BY p.publication_id LIMIT :batch_size
"""
METADATA_BATCH_SIZE = 200
METADATA_ENVELOPE_QUERY = Path(__file__).with_name('metadata_extraction.sql').read_text(encoding='utf-8')
EXTRACTION_BATCH_QUERY = (METADATA_ENVELOPE_QUERY
    .replace('{{publications}}', PUBLICATION_BATCH.replace('{{eligibility}}', 'p.metadata_present IS FALSE'))
    .replace('{{sources}}', SOURCES))
EVALUATION_PUBLICATIONS = PUBLICATION_BATCH.replace('{{eligibility}}', """
    p.metadata_present IS TRUE
    AND (p.inclusion='pending'
         OR (p.inclusion='included' AND p.classification_id IS NULL)
         OR (p.inclusion='excluded' AND p.classification_id IS NOT NULL))
    AND EXISTS (SELECT 1 FROM catalog_publication_languages language
        WHERE language.publication_id=p.publication_id AND language.language=ANY(:languages))
""")
EVALUATION_BATCH_QUERY = (Path(__file__).with_name('metadata_evaluation.sql').read_text(encoding='utf-8')
    .replace('{{envelope}}', METADATA_ENVELOPE_QUERY
        .replace('{{publications}}', EVALUATION_PUBLICATIONS).replace('{{sources}}', SOURCES)))
SOURCE_FIELDS = (
    'md5', 'publication_id', 'revision', 'mime_type', 'selected',
    'primary_revision', 'source_revision', 'content_revision', 'document_url',
    'primary_storage_size', 'content_url', 'source_path', 'upstream_metadata',
)


def checkpoint_identity(publication, source):
    payload = {'publication_id': publication['publication_id'], 'revision': publication['revision'],
               'schema_org': source['schema_org'], **{key: source[key] for key in SOURCE_FIELDS}}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return f"{publication['publication_id']}:{digest}"


def _compose_schema(record, path=None):
    if path is not None:
        record['subjects'] = unmanaged_subjects(record['subjects']) + [
            {'@type': 'DefinedTerm', 'termCode': value,
             'inDefinedTermSet': {'@type': 'DefinedTermSet', 'name': termset}}
            for termset, value in (('DDC', path['ddc']), ('CategoryPath', ' > '.join(path['path_en'])))
        ]
    # Automation reads observed spellings, not an inferred canonical identity.
    return compose_metadata(record)


class MetadataProcessingStore:
    def __init__(self, engine, *, schema):
        self.engine = engine
        self.schema = schema
        self.catalog = CatalogRepository(engine, schema=schema)

    def preflight(self):
        names = ('publications', 'documents', 'locations', 'publication_languages', 'credit_groups',
                 'contributions', 'names', 'entities', 'identifiers', 'genres', 'subjects', 'audiences',
                 'document_access_modes', 'sufficient_modes', 'sufficient_mode_items', 'references',
                 'reference_urls', 'reference_authors', 'protections', 'proposals', 'revisions',
                 'evidence', 'classifications', 'classification_nodes')
        with self.engine.begin() as conn:
            conn.execute(text('SET TRANSACTION READ ONLY'))
            conn.execute(text(configured_timeout_sql("statement_timeout", "preflight_timeout_seconds")))
            inspector = inspect(conn)
            tables = set(inspector.get_table_names(schema=self.schema))
            required = {self.catalog.table(name).name: set(self.catalog.table(name).c.keys()) for name in names}
            required.update(library_upstream_metadata={'md5', 'payload_json'},
                            document_cleanup_queue={'md5', 'scope', 'status'})
            for name, columns in required.items():
                if name not in tables:
                    raise RuntimeError(f'Metadata processing requires catalog table {name}; apply migrations separately')
                actual = {column['name'] for column in inspector.get_columns(name, schema=self.schema)}
                if columns - actual:
                    raise RuntimeError(f'Metadata catalog is missing {name} columns: {sorted(columns - actual)}')
            version_schema = configured_schema("migration_version_schema")
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', version_schema):
                raise ValueError('Invalid migration version schema')
            revision = str(conn.execute(text(f'SELECT version_num FROM "{version_schema}".alembic_version_manzara')).scalar_one())
            if not re.fullmatch(r'\d{8}_\d{4}', revision) or int(revision.split('_')[1]) < 62:
                raise RuntimeError('Metadata processing requires catalog revision 20261008_0062 or later')
            for name, key in (('catalog_documents', ['md5']), ('catalog_publications', ['publication_id'])):
                if inspector.get_pk_constraint(name, schema=self.schema)['constrained_columns'] != key:
                    raise RuntimeError(f'Metadata processing requires the {name} primary key {key}')
            unique = inspector.get_unique_constraints('catalog_locations', schema=self.schema)
            if not any(set(row['column_names']) == {'md5', 'provider', 'purpose'} for row in unique):
                raise RuntimeError('Metadata processing requires unique document/provider/purpose locations')
            conn.execute(text(SOURCES + ' ORDER BY d.md5 LIMIT 1')).mappings().all()

    def _schema(self, conn, source):
        record = self.catalog._metadata(conn, source['md5'], lock=False)
        publication = self.catalog.table('publications')
        classification = conn.execute(select(publication.c.classification_id).where(
            publication.c.publication_id == source['publication_id'])).scalar_one()
        path = None
        if classification is not None:
            table = self.catalog.table('classifications')
            node = conn.execute(select(table.c.node_id).where(table.c.classification_id == classification)).scalar_one()
            path = conn.execute(text('SELECT catalog_path(:node)'), {'node': node}).scalar_one()
        return _compose_schema(record, path)

    def _batch(self, query, should_stop, *, after_id, batch_size, parameters=None):
        """Fetch a bounded candidate/source/metadata envelope in one PostgreSQL statement."""
        integer(after_id, 'after_id', minimum=0)
        integer(batch_size, 'batch_size')
        if batch_size > METADATA_BATCH_SIZE:
            raise ValueError(f'batch_size must not exceed {METADATA_BATCH_SIZE}')
        if should_stop():
            raise InterruptedError('Metadata discovery stopped')
        with self.engine.connect() as conn:
            rows = conn.execute(text(query), {
                'after_id': after_id, 'batch_size': batch_size, 'actors': list(METADATA_TASK_ACTORS),
                **(parameters or {}),
            }).mappings().all()
        return rows

    def _publications(self, rows, should_stop):
        publications = []
        for row in rows:
            if should_stop():
                raise InterruptedError('Metadata discovery stopped')
            publication = dict(row)
            for source in publication['sources']:
                source['save_snapshot'] = {
                    'publication': deepcopy(source['publication_record']),
                    'source': deepcopy(source),
                }
                source['schema_org'] = _compose_schema(deepcopy(source['metadata_record']), source['classification_path'])
                source['checkpoint_id'] = checkpoint_identity(publication, source)
            publication['schema_org'] = publication['sources'][0]['schema_org']
            publications.append(publication)
        return publications

    def extraction_batch(self, should_stop, *, after_id=0, batch_size=METADATA_BATCH_SIZE):
        rows = self._batch(EXTRACTION_BATCH_QUERY, should_stop, after_id=after_id, batch_size=batch_size)
        return self._publications(rows, should_stop)

    def evaluation_batch(self, should_stop, *, languages, after_id=0, batch_size=METADATA_BATCH_SIZE):
        if not isinstance(languages, (list, tuple)) or any(
                not isinstance(language, str) or not language.strip() for language in languages):
            raise ValueError('Evaluation languages must be a list of nonblank language codes')
        rows = self._batch(EVALUATION_BATCH_QUERY, should_stop, after_id=after_id, batch_size=batch_size,
            parameters={'languages': list(languages),
                        'known_classification_limit': config_integer('metadata', 'known_classification_limit')})
        envelope = rows[0]
        return self._publications(envelope['publications'], should_stop), envelope['known_classifications']

    @contextmanager
    def mutation(self, publication, source):
        with self.engine.begin() as conn:
            conn.execute(text('SET TRANSACTION READ WRITE'))
            conn.execute(text(configured_timeout_sql("lock_timeout", "lock_timeout_seconds")))
            document = self.catalog._record(conn, 'document', source['md5'], revision=source['revision'])
            if document['publication_id'] != publication['publication_id']:
                raise CatalogConflict('Evidence document moved to another publication; resume with a fresh snapshot')
            current = self.catalog._record(conn, 'publication', publication['publication_id'], revision=publication['revision'])
            if current['merged_into_id'] is not None:
                raise CatalogConflict('Publication was merged; resume with a fresh inventory')
            locations = self.catalog.table('locations')
            conn.execute(select(locations.c.location_id).where(locations.c.md5 == source['md5'])
                         .order_by(locations.c.location_id).with_for_update()).all()
            fresh = conn.execute(text(SOURCES + ' AND d.md5=:md5'), {'md5': source['md5']}).mappings().one_or_none()
            if fresh is None or any(fresh[key] != source[key] for key in SOURCE_FIELDS):
                raise CatalogConflict('Metadata source changed or became ineligible; resume with a fresh snapshot')
            if self._schema(conn, fresh) != source['schema_org']:
                raise CatalogConflict('Metadata evidence changed; resume with a fresh snapshot')
            yield conn, current

    def save(self, publication, source, schema_org, *, actor, method, evaluation=None):
        snapshot = source.get('save_snapshot')
        if not snapshot or publication['publication_id'] != snapshot['publication']['publication_id']:
            raise CatalogConflict('Metadata save publication does not match the selected snapshot')
        snapshot_query = (METADATA_ENVELOPE_QUERY
            .replace('{{publications}}', "SELECT * FROM catalog_publications WHERE publication_id=publication_key")
            .replace('{{sources}}', SOURCES + ' AND d.md5=document_key'))
        return save_metadata_result(self.engine, self.schema, source, schema_org, actor=actor, method=method,
                                    evaluation=evaluation, snapshot_query=snapshot_query)
