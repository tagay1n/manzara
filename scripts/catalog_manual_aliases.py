"""Offline rendering of the resumable alias and legacy review batch."""

import re

from app.catalog.schema import build_metadata
from scripts.catalog_manual_foundation import _foundation_models, _verify_models
from scripts.catalog_sql import _editor_block


def _alias_models(schema, metadata):
    prefix = 'pg_temp.catalog_manual_expected_'
    models = {}
    for name, key in [('names', 'name_id'), ('aliases', 'alias_id'), ('alias_reviews', 'alias_id')]:
        columns = ','.join(f'"{column.name}"' for column in metadata.tables[f'{schema}.catalog_{name}'].columns
                           if name == 'alias_reviews' or column.name not in {'revision', 'updated_at'})
        models[name] = {'columns': columns, 'key': key,
                        'query': f'SELECT {columns} FROM {prefix}{name}'}
    models['alias_reviews']['ignored_columns'] = ()
    # Later metadata and proposal batches append names after this reserved prefix.
    models['names']['target_query'] = f'''SELECT * FROM "{schema}".catalog_names
        WHERE name_id<=coalesce((SELECT max(name_id) FROM {prefix}names),0)'''
    return models


def _alias_preparations(schema, metadata):
    owner = f'"{schema}"'
    fields = []
    for column in metadata.tables[f'{schema}.catalog_alias_reviews'].columns:
        expression = {'name_id': 'n.name_id', 'entity_id': 'a.canonical_id'}.get(column.name, 'a.' + column.name)
        fields.append(f'{expression} AS "{column.name}"')
    return f"""CREATE TEMP TABLE catalog_manual_alias_walk ON COMMIT DROP AS
    WITH RECURSIVE walk AS (
        SELECT entity_id AS original_id,entity_id,merged_into_id,ARRAY[entity_id] AS visited,false AS cycle
        FROM {owner}.catalog_entities
        UNION ALL
        SELECT w.original_id,e.entity_id,e.merged_into_id,w.visited||e.entity_id,e.entity_id=ANY(w.visited)
        FROM walk w JOIN {owner}.catalog_entities e ON e.entity_id=w.merged_into_id WHERE NOT w.cycle
    ) SELECT * FROM walk;
    IF EXISTS(SELECT 1 FROM pg_temp.catalog_manual_alias_walk WHERE cycle) THEN
        RAISE EXCEPTION 'identity merge cycle before alias loading';
    END IF;
    CREATE TEMP TABLE catalog_manual_alias_survivors ON COMMIT DROP AS
      SELECT original_id,entity_id FROM pg_temp.catalog_manual_alias_walk WHERE merged_into_id IS NULL;
    IF (SELECT count(*) FROM pg_temp.catalog_manual_alias_survivors)<>(SELECT count(*) FROM {owner}.catalog_entities) THEN
        RAISE EXCEPTION 'orphan identity merge target before alias loading';
    END IF;
    CREATE UNIQUE INDEX ON catalog_manual_alias_survivors(original_id);
    CREATE TEMP TABLE catalog_manual_alias_observations ON COMMIT DROP AS
      SELECT a.alias_id,0 AS phase,coalesce(e.kind,CASE a.entity_type WHEN 'personality' THEN 'person' ELSE 'organization' END) AS kind,a.raw_name
      FROM {owner}.normalization_aliases a LEFT JOIN {owner}.catalog_entities e ON e.entity_id=a.canonical_id
      UNION ALL
      SELECT a.alias_id,1,e.kind,a.raw_name FROM {owner}.normalization_aliases a
      JOIN pg_temp.catalog_manual_alias_survivors s ON s.original_id=a.canonical_id
      JOIN {owner}.catalog_entities e ON e.entity_id=s.entity_id WHERE a.decision_status='linked';
    CREATE TEMP TABLE catalog_manual_expected_names ON COMMIT DROP AS
      WITH first_seen AS (
        SELECT DISTINCT ON (kind COLLATE "C",raw_name COLLATE "C") kind,raw_name,alias_id,phase
        FROM pg_temp.catalog_manual_alias_observations ORDER BY kind COLLATE "C",raw_name COLLATE "C",alias_id,phase
      ) SELECT row_number() OVER(ORDER BY alias_id,phase) AS name_id,kind,raw_name FROM first_seen;
    CREATE UNIQUE INDEX ON catalog_manual_expected_names(kind,raw_name);
    CREATE TEMP TABLE catalog_manual_expected_aliases ON COMMIT DROP AS
      WITH pairs AS (
        SELECT n.name_id,e.entity_id,e.approval,min(a.alias_id) AS first_alias
        FROM {owner}.normalization_aliases a
        JOIN pg_temp.catalog_manual_alias_survivors s ON s.original_id=a.canonical_id
        JOIN {owner}.catalog_entities e ON e.entity_id=s.entity_id
        JOIN pg_temp.catalog_manual_expected_names n ON n.kind=e.kind AND n.raw_name=a.raw_name
        WHERE a.decision_status='linked' GROUP BY n.name_id,e.entity_id,e.approval
      ) SELECT row_number() OVER(ORDER BY first_alias,name_id,entity_id) AS alias_id,name_id,entity_id,approval FROM pairs;
    CREATE TEMP TABLE catalog_manual_expected_alias_reviews ON COMMIT DROP AS
      SELECT {','.join(fields)} FROM {owner}.normalization_aliases a
      LEFT JOIN {owner}.catalog_entities e ON e.entity_id=a.canonical_id
      JOIN pg_temp.catalog_manual_expected_names n ON n.raw_name=a.raw_name
        AND n.kind=coalesce(e.kind,CASE a.entity_type WHEN 'personality' THEN 'person' ELSE 'organization' END);
    IF (SELECT count(*) FROM pg_temp.catalog_manual_expected_alias_reviews)<>(SELECT count(*) FROM {owner}.normalization_aliases) THEN
        RAISE EXCEPTION 'alias review mapping is incomplete';
    END IF;"""


def render_manual_aliases(fingerprint, schema='monocorpus', dataset_schema='public', *, aliases_fingerprint):
    metadata = build_metadata(schema)
    build_metadata(dataset_schema)
    if not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
        raise ValueError('invalid reviewed fingerprint')
    if not re.fullmatch(r'[0-9a-f]{64}', aliases_fingerprint):
        raise ValueError('invalid reviewed aliases fingerprint')
    tables = sorted(table.name for table in metadata.tables.values())
    models = _alias_models(schema, metadata)
    verification = _verify_models(schema, models, phase='alias')
    foundation = _verify_models(schema, _foundation_models(schema))
    occupied = '\nUNION ALL '.join(f'SELECT 1 FROM "{schema}"."{name}"' for name in tables if name not in {
        'catalog_imports', 'catalog_collections', 'catalog_entities', 'catalog_entity_roles',
        'catalog_classification_nodes', 'catalog_classifications'})
    inserts = '\n'.join(f'INSERT INTO "{schema}".catalog_{name} ({model["columns"]})\n{model["query"]};'
                        for name, model in models.items())
    sequences = '\n'.join(f"""PERFORM setval(pg_get_serial_sequence('"{schema}".catalog_{name}','{model['key']}'),
        coalesce((SELECT max({model['key']}) FROM "{schema}".catalog_{name}),1),
        EXISTS(SELECT 1 FROM "{schema}".catalog_{name}));""" for name, model in models.items())
    body = f"""DECLARE
    migration "{schema}".catalog_imports;
BEGIN
    PERFORM set_config('lock_timeout','10s',true);
    PERFORM set_config('work_mem','4MB',true);
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
    LOCK TABLE {','.join(f'"{schema}"."{name}"' for name in tables)} IN SHARE ROW EXCLUSIVE MODE;
    LOCK TABLE "{schema}".normalization_aliases,"{schema}".library_collections,
      "{schema}".normalization_canonicals,"{schema}".normalization_events IN SHARE ROW EXCLUSIVE MODE;
    IF (SELECT count(*) FROM "{schema}".normalization_aliases) IS DISTINCT FROM
      (migration.manifest->'source_tables'->'normalization_aliases'->>'rows')::bigint THEN
        RAISE EXCEPTION 'alias count differs from reviewed manifest';
    END IF;
    IF (SELECT encode(sha256(convert_to(coalesce(jsonb_agg(to_jsonb(c) ORDER BY alias_id),'[]')::text,'UTF8')),'hex')
        FROM "{schema}".normalization_aliases c) IS DISTINCT FROM '{aliases_fingerprint}' THEN
        RAISE EXCEPTION 'reviewed alias source changed';
    END IF;
    {foundation}
    IF NOT migration.manifest->'manual_completed_steps' @> '["aliases"]'::jsonb AND
      migration.manifest->'manual_completed_steps' IS DISTINCT FROM '["initialized","foundation","taxonomy"]'::jsonb THEN
        RAISE EXCEPTION 'alias batch is out of order';
    END IF;
    IF EXISTS(SELECT 1 FROM "{schema}".normalization_aliases WHERE entity_type NOT IN ('publisher','personality')) THEN
        RAISE EXCEPTION 'unknown source alias identity type';
    END IF;
    {_alias_preparations(schema, metadata)}
    IF migration.manifest->'manual_completed_steps' @> '["aliases"]'::jsonb THEN
        {verification}
        RETURN;
    END IF;
    IF EXISTS({occupied}) THEN
        RAISE EXCEPTION 'unexpected catalog staging rows before aliases';
    END IF;
    DROP INDEX IF EXISTS "{schema}".idx_catalog_names_raw_name_trgm RESTRICT;
    {inserts}
    {verification}
    {sequences}
    UPDATE "{schema}".catalog_imports SET manifest=jsonb_set(manifest,'{{manual_completed_steps}}',
      manifest->'manual_completed_steps'||'["aliases"]'::jsonb)
      WHERE import_id=migration.import_id;
END;"""
    return _editor_block(body)
