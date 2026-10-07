"""Offline SQL rendering of bounded, restartable document storage locations."""

from app.catalog.schema import build_metadata
from scripts.catalog_manual_documents import (
    SOURCE_TABLES, _document_models, _document_source_preparation,
    _md5, _range, _validate_document_batch, document_source_signature_query,
)
from scripts.catalog_manual_foundation import _verify_models
from scripts.catalog_sql import _editor_block


MAX_LOCATION_BATCH_ROWS = 7500
PREVIOUS_STEPS = '["initialized","foundation","taxonomy","aliases","documents","metadata_details","credits"]'


def _location_model(schema, dataset_schema, metadata, lower_md5, upper_md5):
    source = f'"{dataset_schema}".document d WHERE {_range(lower_md5,upper_md5,"d")}'
    raw = f"""SELECT d.md5,0 AS ordinal,'yandex'::text AS provider,'source'::text AS purpose,
        NULL::text AS locator,d.ya_path AS source_path,d.ya_resource_id AS resource_id,
        d.ya_public_url AS public_url,d.ya_public_key AS public_key,
        NULL::bigint AS size,NULL::text AS etag,NULL::timestamptz AS verified_at
      FROM {source} AND (d.ya_path IS NOT NULL OR d.ya_resource_id IS NOT NULL OR d.ya_public_url IS NOT NULL OR d.ya_public_key IS NOT NULL)
      UNION ALL
      SELECT d.md5,1,'s3','primary',d.document_url,NULL,NULL,NULL,NULL,
        d.primary_storage_size,d.primary_storage_etag,d.primary_storage_verified_at
      FROM {source} AND (d.document_url IS NOT NULL OR d.primary_storage_size IS NOT NULL OR d.primary_storage_etag IS NOT NULL OR d.primary_storage_verified_at IS NOT NULL)
      UNION ALL
      SELECT d.md5,2,'s3','content',d.content_url,NULL,NULL,NULL,NULL,NULL,NULL,NULL
      FROM {source} AND d.content_url IS NOT NULL"""
    prior = '0' if lower_md5 is None else f'(SELECT count(*) FROM "{schema}".catalog_locations WHERE md5<={_md5(lower_md5)})'
    fields = [column.name for column in metadata.tables[f'{schema}.catalog_locations'].columns
              if column.name not in {'revision','updated_at'}]
    projection = ','.join(f'{prior}+row_number() OVER(ORDER BY md5,ordinal) AS location_id' if name=='location_id' else '"'+name+'"' for name in fields)
    preparation = f'CREATE TEMP TABLE catalog_manual_expected_locations ON COMMIT DROP AS WITH raw AS ({raw}) SELECT {projection} FROM raw;'
    models = {'locations': {'columns':','.join('"'+name+'"' for name in fields),'key':'location_id',
        'query':'SELECT * FROM pg_temp.catalog_manual_expected_locations',
        'target_query':f'SELECT * FROM "{schema}".catalog_locations t WHERE {_range(lower_md5,upper_md5,"t")}'}}
    return models, preparation


def render_manual_locations(fingerprint, schema='monocorpus', dataset_schema='public', *, lower_md5, upper_md5,
                            offset, rows, final_batch, source_signatures):
    metadata = build_metadata(schema)
    build_metadata(dataset_schema)
    _validate_document_batch(fingerprint,lower_md5,upper_md5,offset,rows,final_batch,source_signatures)
    if rows>MAX_LOCATION_BATCH_ROWS:
        raise ValueError(f'location batches must not exceed {MAX_LOCATION_BATCH_ROWS} documents')
    models, preparations = _location_model(schema,dataset_schema,metadata,lower_md5,upper_md5)
    core, core_preparations = _document_models(schema,metadata,lower_md5,upper_md5,offset,rows)
    verification = _verify_models(schema,models,phase='location')
    owners = {name:schema if name=='library_collection_items' else dataset_schema for name in SOURCE_TABLES}
    sources = '\n'.join(f"""IF (SELECT count(*) FROM "{owners[name]}"."{name}") IS DISTINCT FROM
        (migration.manifest->'source_tables'->'{name}'->>'rows')::bigint THEN
        RAISE EXCEPTION 'document source count changed for {name}'; END IF;
      IF ({document_source_signature_query(owners[name],name,lower_md5,upper_md5)}) IS DISTINCT FROM '{source_signatures[name]}' THEN
        RAISE EXCEPTION 'document batch source changed for {name}'; END IF;""" for name in SOURCE_TABLES)
    previous = 'NULL' if lower_md5 is None else _md5(lower_md5)
    end_guard = f"""IF {offset+rows}<>(migration.manifest->>'documents')::bigint OR EXISTS(
        SELECT 1 FROM "{dataset_schema}".document WHERE md5>{_md5(upper_md5)}) THEN
        RAISE EXCEPTION 'last location batch does not complete reviewed corpus'; END IF;""" if final_batch else f"""IF {offset+rows}>=(migration.manifest->>'documents')::bigint THEN
        RAISE EXCEPTION 'location completion requires final batch flag'; END IF;"""
    completion = f"""UPDATE "{schema}".catalog_imports SET manifest=jsonb_set(manifest,'{{manual_completed_steps}}',
        manifest->'manual_completed_steps'||'["locations"]'::jsonb) WHERE import_id=migration.import_id;""" if final_batch else ''
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
    LOCK TABLE {','.join('"'+owners[name]+'"."'+name+'"' for name in SOURCE_TABLES)} IN SHARE ROW EXCLUSIVE MODE;
    loaded:=coalesce((migration.manifest->'manual_location_progress'->>'loaded_documents')::bigint,0);
    previous_md5:=migration.manifest->'manual_location_progress'->>'last_md5';
    IF loaded<{offset+rows} AND (loaded<>{offset} OR previous_md5 IS DISTINCT FROM {previous}
      OR migration.manifest->'manual_completed_steps' IS DISTINCT FROM '{PREVIOUS_STEPS}'::jsonb) THEN
        RAISE EXCEPTION 'location batch is out of order'; END IF;
    IF NOT migration.manifest->'manual_completed_steps' @> '["credits"]'::jsonb THEN
        RAISE EXCEPTION 'location batch is out of order'; END IF;
    IF (SELECT count(*) FROM "{schema}".catalog_locations) IS DISTINCT FROM
        coalesce((migration.manifest->'manual_location_progress'->>'loaded_locations')::bigint,0) THEN
        RAISE EXCEPTION 'location staging differs from saved progress'; END IF;
    IF (migration.manifest->'manual_credit_progress'->>'loaded_documents')::bigint IS DISTINCT FROM (migration.manifest->>'documents')::bigint
      OR (SELECT count(*) FROM "{schema}".catalog_contributions) IS DISTINCT FROM (migration.manifest->'manual_credit_progress'->>'loaded_credits')::bigint
      OR (SELECT count(*) FROM "{schema}".catalog_documents)<>(migration.manifest->>'documents')::bigint
      OR (SELECT count(*) FROM "{schema}".catalog_publications)<>(migration.manifest->>'documents')::bigint THEN
        RAISE EXCEPTION 'preceding credit loading is incomplete'; END IF;
    {sources}
    {end_guard}
    {_document_source_preparation(schema,dataset_schema,lower_md5,upper_md5,offset,rows)}
    {core_preparations}
    {_verify_models(schema,core,phase='metadata core')}
    {preparations}
    IF loaded>={offset+rows} THEN
        {verification}
        RETURN;
    END IF;
    INSERT INTO "{schema}".catalog_locations ({models['locations']['columns']})
      SELECT * FROM pg_temp.catalog_manual_expected_locations;
    {verification}
    PERFORM setval(pg_get_serial_sequence('"{schema}".catalog_locations','location_id'),
        coalesce((SELECT max(location_id) FROM "{schema}".catalog_locations),1),EXISTS(SELECT 1 FROM "{schema}".catalog_locations));
    UPDATE "{schema}".catalog_imports SET manifest=jsonb_set(manifest,'{{manual_location_progress}}',
        jsonb_build_object('loaded_documents',{offset+rows},'last_md5',{_md5(upper_md5)},
            'loaded_locations',(SELECT count(*) FROM "{schema}".catalog_locations))) WHERE import_id=migration.import_id;
    {completion}
END;"""
    return _editor_block(body)
