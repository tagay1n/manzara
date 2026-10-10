
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

from sqlalchemy import func, inspect, select, text

from app.catalog.contracts import CatalogConflict, boolean, integer
from app.catalog.metadata import compose_metadata, decompose_metadata
from app.catalog.metadata_store import unmanaged_subjects
from app.catalog.repository import CatalogRepository
from app.catalog.schema_org import is_english_facet, metadata_contract_issues

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
EXTRACTION_PUBLICATIONS = """
 SELECT p.* FROM catalog_publications p
 WHERE p.merged_into_id IS NULL AND p.metadata_present IS FALSE
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
EXTRACTION_BATCH_SIZE = 200
EXTRACTION_BATCH_QUERY = (Path(__file__).with_name('metadata_extraction.sql').read_text(encoding='utf-8')
    .replace('{{publications}}', EXTRACTION_PUBLICATIONS).replace('{{sources}}', SOURCES))
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

    def extraction_batch(self, should_stop, *, after_id=0, batch_size=EXTRACTION_BATCH_SIZE):
        """Fetch a bounded candidate/source/metadata envelope in one PostgreSQL statement."""
        integer(after_id, 'after_id', minimum=0)
        integer(batch_size, 'batch_size')
        if batch_size > EXTRACTION_BATCH_SIZE:
            raise ValueError(f'batch_size must not exceed {EXTRACTION_BATCH_SIZE}')
        if should_stop():
            raise InterruptedError('Metadata discovery stopped')
        with self.engine.connect() as conn:
            rows = conn.execute(text(EXTRACTION_BATCH_QUERY), {
                'after_id': after_id, 'batch_size': batch_size, 'actors': list(METADATA_TASK_ACTORS),
            }).mappings().all()
        publications = []
        for row in rows:
            if should_stop():
                raise InterruptedError('Metadata discovery stopped')
            publication = dict(row)
            for source in publication['sources']:
                record = source.pop('metadata_record')
                source['schema_org'] = _compose_schema(record, source.pop('classification_path'))
                source['checkpoint_id'] = checkpoint_identity(publication, source)
            publication['schema_org'] = publication['sources'][0]['schema_org']
            publications.append(publication)
        return publications

    def inventory(self, should_stop):
        publications = self.catalog.table('publications')
        with self.engine.begin() as conn:
            conn.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            rows = [dict(row) for row in conn.execute(select(publications).where(
                publications.c.merged_into_id.is_(None)).order_by(publications.c.publication_id)).mappings()]
            by_id = {row['publication_id']: {**row, 'sources': []} for row in rows}
            for source in conn.execute(text(SOURCES + ' ORDER BY d.publication_id, d.selected DESC, d.md5')).mappings():
                if should_stop():
                    raise InterruptedError('Metadata inventory stopped')
                if source['publication_id'] not in by_id:
                    continue
                source = dict(source)
                source['schema_org'] = self._schema(conn, source)
                publication = by_id[source['publication_id']]
                source['checkpoint_id'] = checkpoint_identity(publication, source)
                publication['sources'].append(source)
            documents = self.catalog.table('documents')
            for document in conn.execute(select(documents.c.md5, documents.c.publication_id)
                    .order_by(documents.c.selected.desc(), documents.c.md5)).mappings():
                publication = by_id.get(document['publication_id'])
                if publication is not None and 'schema_org' not in publication:
                    if should_stop():
                        raise InterruptedError('Metadata inventory stopped')
                    publication['schema_org'] = (publication['sources'][0]['schema_org'] if publication['sources']
                                                 else self._schema(conn, document))
            languages = self.catalog.table('publication_languages')
            for row in conn.execute(select(languages).order_by(languages.c.position)).mappings():
                if row['publication_id'] in by_id:
                    by_id[row['publication_id']].setdefault('languages', []).append(row['language'])
            protections = self.catalog.table('protections')
            for row in conn.execute(select(protections).where(protections.c.record_kind == 'publication')).mappings():
                key = int(row['record_key'])
                if key in by_id:
                    by_id[key].setdefault('protections', set()).add(row['field'])
            proposals = self.catalog.table('proposals')
            for row in conn.execute(select(proposals).where(proposals.c.kind == 'metadata',
                    proposals.c.status.in_(('pending', 'deferred')))).mappings():
                if row['publication_id'] in by_id and row['evidence'].get('actor') in METADATA_TASK_ACTORS:
                    by_id[row['publication_id']]['awaiting_review'] = True
            return list(by_id.values())

    def known_classifications(self, limit=None):
        if limit is None:
            limit = config_integer("metadata", "known_classification_limit")
        with self.engine.begin() as conn:
            conn.execute(text('SET TRANSACTION READ ONLY'))
            return [dict(row) for row in conn.execute(text('''
                SELECT c.classification_id AS id, catalog_path(c.node_id)->>'ddc' AS ddc,
                       catalog_path(c.node_id)->'path_en' AS path
                FROM catalog_classifications c LEFT JOIN catalog_publications p USING(classification_id)
                GROUP BY c.classification_id, c.node_id
                ORDER BY COUNT(p.publication_id) DESC, ddc, c.classification_id LIMIT :limit
            '''), {'limit': limit}).mappings()]

    @contextmanager
    def mutation(self, publication, source, *, taxonomy=False):
        with self.engine.begin() as conn:
            conn.execute(text('SET TRANSACTION READ WRITE'))
            conn.execute(text(configured_timeout_sql("lock_timeout", "lock_timeout_seconds")))
            if taxonomy:
                conn.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'),
                             {'key': f'catalog-taxonomy:{self.schema}'})
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

    def _proposal(self, conn, publication, source, changes, actor):
        return conn.execute(self.catalog.table('proposals').insert().values(
            kind='metadata', publication_id=publication['publication_id'],
            evidence={'actor': actor, 'md5': source['md5'], 'publication_revision': publication['revision'],
                      'document_revision': source['revision']}, field_changes=changes,
        ).returning(self.catalog.table('proposals').c.proposal_id)).scalar_one()

    def _decision(self, conn, publication, source, changes, actor):
        changes = {key: value for key, value in changes.items() if publication[key] != value}
        protected = self.catalog._protected(conn, 'publication', publication['publication_id'])
        # Inclusion/classification/method are one reviewed decision, never a partial update.
        if changes and protected & {'inclusion', 'classification_id', 'evaluation_method'}:
            return self._proposal(conn, publication, source, changes, actor)
        if changes:
            self.catalog._update(conn, 'publication', publication['publication_id'], publication, changes, actor)
        return None

    def save(self, publication, source, schema_org, *, actor, method, evaluation=None):
        incoming = decompose_metadata(deepcopy(schema_org))
        incoming['subjects'] = unmanaged_subjects(incoming['subjects'])
        with self.mutation(publication, source, taxonomy=evaluation is not None) as (conn, current):
            if evaluation is None and current['metadata_present']:
                raise CatalogConflict('Publication already has metadata; extraction only fills missing metadata')
            before_proposals = self._proposal_ids(conn, publication['publication_id'])
            if evaluation is None:
                # An earlier automatically derived decision no longer describes the new metadata.
                if current['evaluation_method'] is not None:
                    self._decision(conn, current, source, {'inclusion': 'pending', 'classification_id': None,
                        'evaluation_method': None}, actor)
                    current = self.catalog._record(conn, 'publication', publication['publication_id'])
            self.catalog._apply_metadata(conn, source['md5'], incoming, schema_org, actor, True, current['revision'])
            if evaluation is None:
                doc = self.catalog._record(conn, 'document', source['md5'])
                self.catalog._update(conn, 'document', source['md5'], doc, {'meta_extraction_method': method}, actor)
            else:
                applicable = boolean(evaluation['applicable'], 'applicable')
                classification = self._classification(conn, evaluation['ddc'], evaluation['path'], actor) if applicable else None
                current = self.catalog._record(conn, 'publication', publication['publication_id'])
                self._decision(conn, current, source, {'inclusion': 'included' if applicable else 'excluded',
                    'classification_id': classification, 'evaluation_method': method}, actor)
                conn.execute(self.catalog.table('evidence').insert().values(md5=source['md5'], record_kind='publication',
                    record_key=str(publication['publication_id']), source=actor, payload=evaluation))
            # Validate actual retained fields after protections, not just the incoming payload.
            fresh = self._schema(conn, source)
            proposal_ids = sorted(self._proposal_ids(conn, publication['publication_id']) - before_proposals)
            if (issues := metadata_contract_issues(fresh)) and not proposal_ids:
                raise CatalogConflict('Protected or retained metadata requires review: ' +
                                      ', '.join(sorted({issue['code'] for issue in issues})))
            return proposal_ids

    def _proposal_ids(self, conn, publication_id):
        table = self.catalog.table('proposals')
        return set(conn.execute(select(table.c.proposal_id).where(table.c.publication_id == publication_id)).scalars())

    def _classification(self, conn, ddc, path, actor):
        if not isinstance(ddc, str) or not re.fullmatch(r'\d{3}(?:\.\d+)?', ddc):
            raise ValueError('Applicable evaluation requires a valid DDC')
        if not isinstance(path, list) or not 2 <= len(path) <= 8 or any(not is_english_facet(label) for label in path):
            raise ValueError('Applicable evaluation requires a valid category path')
        conn.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'), {'key': f'catalog-taxonomy:{self.schema}'})
        nodes = self.catalog.table('classification_nodes')
        parent = None
        for label in path:
            row = conn.execute(select(nodes).where(nodes.c.ddc == ddc, func.lower(nodes.c.label_en) == label.casefold(),
                nodes.c.parent_id.is_(None) if parent is None else nodes.c.parent_id == parent)
                .order_by(nodes.c.node_id).limit(1)).mappings().one_or_none()
            if row is None:
                row = conn.execute(nodes.insert().values(ddc=ddc, label_en=label, parent_id=parent).returning(nodes)).mappings().one()
                self.catalog._audit(conn, 'classification_node', row['node_id'], None, dict(row), actor)
            parent = row['node_id']
        table = self.catalog.table('classifications')
        row = conn.execute(select(table).where(table.c.node_id == parent)).mappings().one_or_none()
        if row is None:
            row = conn.execute(table.insert().values(node_id=parent, status='pending', created_by='gemini').returning(table)).mappings().one()
            self.catalog._audit(conn, 'classification', row['classification_id'], None, dict(row), actor)
        return row['classification_id']
