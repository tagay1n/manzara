"""Offline SQL rendering for the first resumable catalog data batch."""

import re

from app.catalog.schema import build_metadata
from scripts.catalog_sql import _editor_block


def _foundation_models(schema):
    owner = f'"{schema}"'
    collections = {
        'collection_id': 'c.collection_id', 'title': 'c.title', 'notes': 'c.notes',
        'include_in_library': 'c.include_in_library=1',
        'metadata_template_json': 'c.metadata_template_json', 'applied_at': 'c.applied_at',
        'created_at': 'c.created_at', 'normalized_title': 'c.normalized_title',
        'source_updated_at': 'c.updated_at',
    }
    approvals = f"""WITH events AS (
    SELECT coalesce(nullif(payload_json,''),'{{}}')::jsonb AS payload
    FROM {owner}.normalization_events
    WHERE reverted=0 AND action IN ('apply_publisher_change_set','apply_personality_change_set')
), approved AS (
    SELECT c.canonical_id FROM events e
    CROSS JOIN LATERAL jsonb_array_elements(coalesce(e.payload->'after'->'canonicals','[]')) x
    JOIN {owner}.normalization_canonicals c ON to_jsonb(c.canonical_id)=x.value->'canonical_id'
      AND to_jsonb(c.display_name)=x.value->'display_name'
    WHERE coalesce(e.payload->'touched_canonical_ids','[]') @> jsonb_build_array(x.value->'canonical_id')
    UNION
    SELECT c.canonical_id FROM events e
    CROSS JOIN LATERAL jsonb_array_elements(coalesce(e.payload->'renames','[]')) x
    JOIN {owner}.normalization_canonicals c ON to_jsonb(c.canonical_id)=x.value->'canonical_id'
      AND to_jsonb(c.display_name)=x.value->'display_name'
)"""
    entities = {
        'entity_id': 'c.canonical_id',
        'kind': "CASE c.entity_type WHEN 'personality' THEN 'person' ELSE 'organization' END",
        'display_name': 'c.display_name',
        'approval': "CASE WHEN EXISTS(SELECT 1 FROM approved a WHERE a.canonical_id=c.canonical_id) THEN 'confirmed' ELSE 'unconfirmed' END",
        'normalized_name': 'c.normalized_name', 'created_at': 'c.created_at',
        'source_updated_at': 'c.updated_at', 'status': 'c.status', 'merged_into_id': 'c.merged_into_id',
        **{name: 'c.' + name for name in ('surname_full', 'surname_initials', 'name_full', 'name_initials',
            'father_name_full', 'father_name_initials', 'title', 'sex', 'identity_key', 'notes')},
    }
    roles = {'entity_id': 'c.canonical_id',
             'role': "CASE c.entity_type WHEN 'publisher' THEN 'publisher' ELSE 'personality' END"}

    def model(fields, source, *, prefix='', key):
        projection = ',\n    '.join(f'{value} AS "{name}"' for name, value in fields.items())
        return {'columns': ','.join(f'"{name}"' for name in fields),
                'query': f'{prefix}\nSELECT {projection} FROM {owner}."{source}" c', 'key': key}

    return {
        'collections': model(collections, 'library_collections', key='collection_id'),
        'entities': model(entities, 'normalization_canonicals', prefix=approvals, key='entity_id'),
        'entity_roles': model(roles, 'normalization_canonicals', key='entity_id,role'),
    }


def _verify_models(schema, models, *, phase='foundation'):
    statements = []
    for name, model in models.items():
        key = model['key'].split(',')[0]
        ignored = ','.join("'" + column + "'" for column in model.get('ignored_columns', ('revision', 'updated_at')))
        target = f'"{schema}".catalog_{name}'
        if model.get('target_query'):
            target = '(' + model['target_query'] + ')'
        statements.append(f"""IF EXISTS (
    WITH expected AS ({model['query']})
    SELECT 1 FROM expected src FULL JOIN {target} target USING ({model['key']})
    WHERE src."{key}" IS NULL OR target."{key}" IS NULL
      OR (to_jsonb(target)-ARRAY[{ignored}]::text[]) IS DISTINCT FROM to_jsonb(src)
) THEN
    RAISE EXCEPTION '{phase} mapping differs for {name}';
END IF;""")
    return '\n'.join(statements)


def render_manual_foundation(fingerprint, schema='monocorpus', dataset_schema='public'):
    tables = sorted(table.name for table in build_metadata(schema).tables.values())
    build_metadata(dataset_schema)
    if not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
        raise ValueError('invalid reviewed fingerprint')
    models = _foundation_models(schema)
    verification = _verify_models(schema, models)
    relations = ','.join(f'"{schema}"."{name}"' for name in tables)
    occupied = '\nUNION ALL '.join(f'SELECT 1 FROM "{schema}"."{name}"'
                                  for name in tables if name != 'catalog_imports')
    sources = ('library_collections', 'normalization_canonicals', 'normalization_events')
    count_checks = '\n'.join(f"""IF (SELECT count(*) FROM "{schema}"."{name}") <>
    (migration.manifest->'source_tables'->'{name}'->>'rows')::bigint THEN
    RAISE EXCEPTION 'source count changed for {name}';
END IF;""" for name in sources)
    inserts = '\n'.join(f'INSERT INTO "{schema}".catalog_{name} ({model["columns"]})\n{model["query"]};'
                        for name, model in models.items())
    sequences = '\n'.join(f"""PERFORM setval(pg_get_serial_sequence('"{schema}".catalog_{name}','{key}'),
    coalesce((SELECT max({key}) FROM "{schema}".catalog_{name}),1),
    EXISTS(SELECT 1 FROM "{schema}".catalog_{name}));"""
                          for name, key in (('entities', 'entity_id'), ('collections', 'collection_id')))
    body = f"""DECLARE
    migration "{schema}".catalog_imports;
BEGIN
    PERFORM set_config('lock_timeout','10s',true);
    PERFORM set_config('maintenance_work_mem','16MB',true);
    IF NOT pg_try_advisory_xact_lock(hashtext('catalog-import:{schema}')) THEN
        RAISE EXCEPTION 'another catalog migration is running';
    END IF;
    SELECT * INTO migration FROM "{schema}".catalog_imports
      WHERE source_fingerprint='{fingerprint}' FOR UPDATE;
    IF migration.import_id IS NULL OR migration.state<>'loading'
      OR migration.manifest->'manual_editor' IS DISTINCT FROM 'true'::jsonb
      OR migration.manifest->>'evidence_mode' IS DISTINCT FROM 'essential'
      OR migration.manifest->>'dataset_schema' IS DISTINCT FROM '{dataset_schema}' THEN
        RAISE EXCEPTION 'reviewed manual loading import required';
    END IF;
    LOCK TABLE {relations} IN SHARE ROW EXCLUSIVE MODE;
    LOCK TABLE {','.join(f'"{schema}"."{name}"' for name in sources)} IN SHARE ROW EXCLUSIVE MODE;
    {count_checks}
    IF migration.manifest->'manual_completed_steps' @> '["foundation"]'::jsonb THEN
        {verification}
        RETURN;
    END IF;
    IF migration.manifest->'manual_completed_steps' IS DISTINCT FROM '["initialized"]'::jsonb THEN
        RAISE EXCEPTION 'foundation batch is out of order';
    END IF;
    IF EXISTS({occupied}) THEN
        RAISE EXCEPTION 'catalog staging contains rows';
    END IF;
    IF EXISTS(SELECT 1 FROM "{schema}".library_collections WHERE include_in_library IS NULL OR include_in_library NOT IN (0,1)) THEN
        RAISE EXCEPTION 'invalid collection inclusion flag';
    END IF;
    IF EXISTS(SELECT 1 FROM "{schema}".normalization_canonicals WHERE entity_type NOT IN ('publisher','personality')) THEN
        RAISE EXCEPTION 'unknown source identity type';
    END IF;
    DROP INDEX IF EXISTS "{schema}".idx_catalog_collections_title_trgm RESTRICT;
    DROP INDEX IF EXISTS "{schema}".idx_catalog_entities_display_name_trgm RESTRICT;
    {inserts}
    {verification}
    IF migration.manifest->'confirmed_entities' IS NULL OR
      (SELECT count(*) FROM "{schema}".catalog_entities WHERE approval='confirmed') <>
      (migration.manifest->>'confirmed_entities')::bigint THEN
        RAISE EXCEPTION 'human confirmation count differs from reviewed manifest';
    END IF;
    {sequences}
    UPDATE "{schema}".catalog_imports SET manifest=jsonb_set(manifest,'{{manual_completed_steps}}',
      manifest->'manual_completed_steps'||'["foundation"]'::jsonb)
      WHERE import_id=migration.import_id;
END;"""
    return _editor_block(body)
