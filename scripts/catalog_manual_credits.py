"""Offline SQL rendering for restartable bibliographic credits and observed names."""

import re

from app.catalog.metadata import RELATIONS
from app.catalog.schema import build_metadata
from scripts.catalog_manual_aliases import _alias_models, _alias_preparations
from scripts.catalog_manual_documents import (
    SOURCE_TABLES, _document_models, _document_source_preparation, _items,
    _md5, _validate_document_batch, document_source_signature_query,
)
from scripts.catalog_manual_foundation import _foundation_models, _verify_models
from scripts.catalog_sql import _editor_block


PREVIOUS_STEPS = '["initialized","foundation","taxonomy","aliases","documents","metadata_details"]'


def _credit_source():
    roles = ','.join("('" + role + "')" for role in sorted(RELATIONS))
    nested = "CASE WHEN item.value->>'@type'='Role' THEN item.value->'contributor' ELSE item.value END"
    return f"""CREATE TEMP TABLE catalog_manual_credit_source ON COMMIT DROP AS
      SELECT s.publication_id,r.role,
        CASE WHEN item.value->>'@type'='Role' THEN item.value->>'roleName' END AS role_name,
        (item.ordinal-1)::integer AS position,(entity.ordinal-1)::integer AS nested_position,
        lower(entity.value->>'@type') AS kind,entity.value->>'name' AS raw_name,
        entity.value AS source_entity
      FROM pg_temp.catalog_manual_document_source s CROSS JOIN (VALUES {roles}) AS r(role)
      CROSS JOIN LATERAL jsonb_array_elements({_items('s.payload->r.role')}) WITH ORDINALITY AS item(value,ordinal)
      CROSS JOIN LATERAL jsonb_array_elements({_items(nested)}) WITH ORDINALITY AS entity(value,ordinal);
    IF EXISTS(SELECT 1 FROM pg_temp.catalog_manual_credit_source WHERE kind IS NULL OR kind NOT IN ('person','organization')
      OR raw_name IS NULL OR jsonb_typeof(source_entity->'name') IS DISTINCT FROM 'string'
      OR jsonb_typeof(source_entity) IS DISTINCT FROM 'object'
      OR (source_entity-ARRAY['@type','name'])<>'{{}}'::jsonb) THEN
        RAISE EXCEPTION 'invalid credited entity'; END IF;
    CREATE INDEX ON catalog_manual_credit_source(kind,raw_name);"""


def _credit_model(schema, metadata, offset, rows):
    fields = {
        'contribution_id': f'''(SELECT count(*) FROM "{schema}".catalog_contributions WHERE publication_id<={offset})+
            row_number() OVER(ORDER BY c.publication_id,c.role COLLATE "C",c.position,c.nested_position)''',
        'publication_id': 'c.publication_id', 'name_id': 'n.name_id', 'entity_id': 'a.entity_id',
        'role': 'c.role', 'role_name': 'c.role_name', 'position': 'c.position', 'nested_position': 'c.nested_position',
        'resolution': "coalesce(e.approval,'unconfirmed')",
    }
    columns = [column.name for column in metadata.tables[f'{schema}.catalog_contributions'].columns
               if column.name not in {'revision','updated_at'}]
    query = f'''SELECT {','.join(fields[name] + ' AS "' + name + '"' for name in columns)}
        FROM pg_temp.catalog_manual_credit_source c JOIN "{schema}".catalog_names n USING(kind,raw_name)
        LEFT JOIN "{schema}".catalog_aliases a USING(name_id)
        LEFT JOIN "{schema}".catalog_entities e USING(entity_id)'''
    return {'contributions': {'columns': ','.join('"' + name + '"' for name in columns), 'key': 'contribution_id',
        'query': 'SELECT * FROM pg_temp.catalog_manual_expected_contributions',
        'target_query': f'SELECT * FROM "{schema}".catalog_contributions WHERE publication_id>{offset} AND publication_id<={offset+rows}'}}, query


def render_manual_credits(fingerprint, schema='monocorpus', dataset_schema='public', *, lower_md5, upper_md5,
                          offset, rows, final_batch, source_signatures, aliases_fingerprint):
    metadata = build_metadata(schema)
    build_metadata(dataset_schema)
    _validate_document_batch(fingerprint,lower_md5,upper_md5,offset,rows,final_batch,source_signatures)
    if not isinstance(aliases_fingerprint,str) or not re.fullmatch(r'[0-9a-f]{64}',aliases_fingerprint):
        raise ValueError('invalid reviewed aliases fingerprint')
    models, credit_query = _credit_model(schema,metadata,offset,rows)
    core, core_preparations = _document_models(schema,metadata,lower_md5,upper_md5,offset,rows)
    verification = _verify_models(schema,models,phase='credit')
    owners = {name:schema if name=='library_collection_items' else dataset_schema for name in SOURCE_TABLES}
    sources = '\n'.join(f"""IF (SELECT count(*) FROM "{owners[name]}"."{name}") IS DISTINCT FROM
        (migration.manifest->'source_tables'->'{name}'->>'rows')::bigint THEN
        RAISE EXCEPTION 'document source count changed for {name}'; END IF;
      IF ({document_source_signature_query(owners[name],name,lower_md5,upper_md5)}) IS DISTINCT FROM '{source_signatures[name]}' THEN
        RAISE EXCEPTION 'document batch source changed for {name}'; END IF;""" for name in SOURCE_TABLES)
    previous = 'NULL' if lower_md5 is None else _md5(lower_md5)
    completion = f"""UPDATE "{schema}".catalog_imports SET manifest=jsonb_set(manifest,'{{manual_completed_steps}}',
        manifest->'manual_completed_steps'||'["credits"]'::jsonb) WHERE import_id=migration.import_id;""" if final_batch else ''
    end_guard = f"""IF {offset+rows}<>(migration.manifest->>'documents')::bigint OR EXISTS(
        SELECT 1 FROM "{dataset_schema}".document WHERE md5>{_md5(upper_md5)}) THEN
        RAISE EXCEPTION 'last credit batch does not complete reviewed corpus'; END IF;""" if final_batch else f"""IF {offset+rows}>=(migration.manifest->>'documents')::bigint THEN
        RAISE EXCEPTION 'credit completion requires final batch flag'; END IF;"""
    body = f"""DECLARE
    migration "{schema}".catalog_imports;
    loaded bigint;
    previous_md5 text;
BEGIN
    PERFORM set_config('lock_timeout','10s',true);
    PERFORM set_config('work_mem','4MB',true);
    PERFORM set_config('maintenance_work_mem','16MB',true);
    PERFORM set_config('TimeZone','UTC',true);
    IF NOT pg_try_advisory_xact_lock(hashtext('catalog-import:{schema}')) THEN
        RAISE EXCEPTION 'another catalog migration is running'; END IF;
    SELECT * INTO migration FROM "{schema}".catalog_imports WHERE source_fingerprint='{fingerprint}' FOR UPDATE;
    IF migration.import_id IS NULL OR migration.state<>'loading'
      OR migration.manifest->'manual_editor' IS DISTINCT FROM 'true'::jsonb
      OR migration.manifest->>'evidence_mode' IS DISTINCT FROM 'essential'
      OR migration.manifest->>'dataset_schema' IS DISTINCT FROM '{dataset_schema}' THEN
        RAISE EXCEPTION 'reviewed manual loading import required'; END IF;
    LOCK TABLE {','.join('"'+schema+'"."'+table.name+'"' for table in sorted(metadata.tables.values(),key=lambda t:t.name))} IN SHARE ROW EXCLUSIVE MODE;
    LOCK TABLE {','.join('"'+owners[name]+'"."'+name+'"' for name in SOURCE_TABLES)},
      "{schema}".normalization_aliases,"{schema}".normalization_canonicals,
      "{schema}".normalization_events,"{schema}".library_collections IN SHARE ROW EXCLUSIVE MODE;
    loaded:=coalesce((migration.manifest->'manual_credit_progress'->>'loaded_documents')::bigint,0);
    previous_md5:=migration.manifest->'manual_credit_progress'->>'last_md5';
    IF loaded<{offset+rows} AND (loaded<>{offset} OR previous_md5 IS DISTINCT FROM {previous}
      OR migration.manifest->'manual_completed_steps' IS DISTINCT FROM '{PREVIOUS_STEPS}'::jsonb) THEN
        RAISE EXCEPTION 'credit batch is out of order'; END IF;
    IF NOT migration.manifest->'manual_completed_steps' @> '["metadata_details"]'::jsonb THEN
        RAISE EXCEPTION 'credit batch is out of order'; END IF;
    IF (SELECT count(*) FROM "{schema}".catalog_contributions) IS DISTINCT FROM
        coalesce((migration.manifest->'manual_credit_progress'->>'loaded_credits')::bigint,0) THEN
        RAISE EXCEPTION 'credit staging differs from saved progress'; END IF;
    IF (migration.manifest->'manual_details_progress'->>'loaded_documents')::bigint IS DISTINCT FROM (migration.manifest->>'documents')::bigint
      OR (SELECT count(*) FROM "{schema}".catalog_documents)<>(migration.manifest->>'documents')::bigint
      OR (SELECT count(*) FROM "{schema}".catalog_publications)<>(migration.manifest->>'documents')::bigint THEN
        RAISE EXCEPTION 'preceding document loading is incomplete'; END IF;
    {sources}
    {end_guard}
    IF (SELECT count(*) FROM "{schema}".normalization_aliases) IS DISTINCT FROM
      (migration.manifest->'source_tables'->'normalization_aliases'->>'rows')::bigint THEN
        RAISE EXCEPTION 'alias count differs from reviewed manifest'; END IF;
    IF (SELECT encode(sha256(convert_to(coalesce(jsonb_agg(to_jsonb(c) ORDER BY alias_id),'[]')::text,'UTF8')),'hex')
        FROM "{schema}".normalization_aliases c) IS DISTINCT FROM '{aliases_fingerprint}' THEN
        RAISE EXCEPTION 'reviewed alias source changed'; END IF;
    {_verify_models(schema,_foundation_models(schema))}
    {_alias_preparations(schema,metadata)}
    {_verify_models(schema,_alias_models(schema,metadata),phase='alias')}
    IF EXISTS(SELECT name_id FROM "{schema}".catalog_aliases GROUP BY name_id HAVING count(*)>1) THEN
        RAISE EXCEPTION 'ambiguous alias resolution requires review'; END IF;
    {_document_source_preparation(schema,dataset_schema,lower_md5,upper_md5,offset,rows)}
    {core_preparations}
    {_verify_models(schema,core,phase='metadata core')}
    {_credit_source()}
    IF loaded<{offset+rows} THEN
        IF loaded=0 AND EXISTS(SELECT 1 FROM "{schema}".catalog_names WHERE name_id>
            coalesce((SELECT max(name_id) FROM pg_temp.catalog_manual_expected_names),0)) THEN
            RAISE EXCEPTION 'unexpected names before credit loading'; END IF;
        CREATE TEMP TABLE catalog_manual_new_credit_names ON COMMIT DROP AS
          WITH missing AS (
            SELECT DISTINCT ON (c.kind COLLATE "C",c.raw_name COLLATE "C") c.kind,c.raw_name,
                c.publication_id,c.role,c.position,c.nested_position
            FROM pg_temp.catalog_manual_credit_source c
            WHERE NOT EXISTS(SELECT 1 FROM "{schema}".catalog_names n WHERE n.kind=c.kind AND n.raw_name=c.raw_name)
            ORDER BY c.kind COLLATE "C",c.raw_name COLLATE "C",c.publication_id,c.role COLLATE "C",c.position,c.nested_position
          ) SELECT (SELECT coalesce(max(name_id),0) FROM "{schema}".catalog_names)+
            row_number() OVER(ORDER BY publication_id,role COLLATE "C",position,nested_position) AS name_id,kind,raw_name FROM missing;
        INSERT INTO "{schema}".catalog_names(name_id,kind,raw_name)
          SELECT name_id,kind,raw_name FROM pg_temp.catalog_manual_new_credit_names;
    END IF;
    IF EXISTS(SELECT 1 FROM pg_temp.catalog_manual_credit_source c WHERE NOT EXISTS(
        SELECT 1 FROM "{schema}".catalog_names n WHERE n.kind=c.kind AND n.raw_name=c.raw_name)) THEN
        RAISE EXCEPTION 'missing credit name mapping'; END IF;
    CREATE TEMP TABLE catalog_manual_expected_contributions ON COMMIT DROP AS {credit_query};
    IF (SELECT count(*) FROM pg_temp.catalog_manual_expected_contributions)<>(SELECT count(*) FROM pg_temp.catalog_manual_credit_source) THEN
        RAISE EXCEPTION 'credit expansion differs from source'; END IF;
    IF loaded>={offset+rows} THEN
        {verification}
        RETURN;
    END IF;
    INSERT INTO "{schema}".catalog_contributions ({models['contributions']['columns']})
      SELECT * FROM pg_temp.catalog_manual_expected_contributions;
    {verification}
    PERFORM setval(pg_get_serial_sequence('"{schema}".catalog_names','name_id'),
        coalesce((SELECT max(name_id) FROM "{schema}".catalog_names),1),EXISTS(SELECT 1 FROM "{schema}".catalog_names));
    PERFORM setval(pg_get_serial_sequence('"{schema}".catalog_contributions','contribution_id'),
        coalesce((SELECT max(contribution_id) FROM "{schema}".catalog_contributions),1),EXISTS(SELECT 1 FROM "{schema}".catalog_contributions));
    UPDATE "{schema}".catalog_imports SET manifest=jsonb_set(manifest,'{{manual_credit_progress}}',
        jsonb_build_object('loaded_documents',{offset+rows},'last_md5',{_md5(upper_md5)},
            'loaded_credits',(SELECT count(*) FROM "{schema}".catalog_contributions),
            'loaded_names',(SELECT count(*) FROM "{schema}".catalog_names))) WHERE import_id=migration.import_id;
    {completion}
END;"""
    return _editor_block(body)
