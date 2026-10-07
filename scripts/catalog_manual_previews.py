"""Offline, resumable preview import that blocks private legacy public assets."""

import re

from app.catalog.schema import build_metadata
from scripts.catalog_manual_foundation import _verify_models
from scripts.catalog_sql import _editor_block


SOURCE_TABLES = ('document', 'library_book_previews')
PREVIOUS_STEPS = '["initialized","foundation","taxonomy","aliases","documents","metadata_details","credits","locations"]'


def preview_source_signature_query(schema, table):
    build_metadata(schema)
    if table not in SOURCE_TABLES:
        raise ValueError('unsupported preview source table')
    return f"""SELECT encode(sha256(convert_to(coalesce(string_agg(
        encode(sha256(convert_to(to_jsonb(c)::text,'UTF8')),'hex'),'' ORDER BY md5),''),'UTF8')),'hex')
        FROM "{schema}"."{table}" c"""


def _preview_models(schema, dataset_schema, metadata):
    preparation = f"""CREATE TEMP TABLE catalog_manual_preview_source ON COMMIT DROP AS
        SELECT p.*,row_number() OVER(ORDER BY p.md5) AS request_id,
            d.sharing_restricted IS NOT FALSE AS legacy_private
        FROM "{schema}".library_book_previews p JOIN "{dataset_schema}".document d USING(md5);"""
    fields = {
        'request_id':'s.request_id','md5':'s.md5',"idempotency_key":"'legacy:'||s.md5",
        'recipe':'s.recipe_version',
        'status':"CASE WHEN s.legacy_private THEN 'failed' WHEN s.status='processing' THEN 'pending' ELSE s.status END",
        'private':'false','actor':"migration.manifest->>'actor'",'claim_token':'NULL::text',
        'lease_until':'NULL::timestamptz','source_page_count':'s.source_page_count',
        'error':"CASE WHEN s.legacy_private THEN 'Legacy public assets require regeneration into private storage' ELSE s.error_text END",
    }
    columns = [column.name for column in metadata.tables[f'{schema}.catalog_preview_requests'].columns
               if column.name != 'created_at']
    projection = ','.join(f'{fields[name]} AS "{name}"' for name in columns)
    preparation += f"""\nCREATE TEMP TABLE catalog_manual_expected_preview_requests ON COMMIT DROP AS
        SELECT {projection} FROM pg_temp.catalog_manual_preview_source s;
    CREATE TEMP TABLE catalog_manual_expected_preview_pages ON COMMIT DROP AS
        SELECT s.request_id,v.role,v.page_number,s.md5||'/'||v.alias||'s.webp' AS small_key,
            s.md5||'/'||v.alias||'l.webp' AS large_key
        FROM pg_temp.catalog_manual_preview_source s CROSS JOIN LATERAL (VALUES
            ('first','1',s.first_preview_page),('second','2',s.second_preview_page),
            ('last','l',s.last_preview_page)) v(role,alias,page_number)
        WHERE v.page_number IS NOT NULL AND NOT s.legacy_private;"""
    models = {
        'preview_requests':{'key':'request_id','columns':','.join('"'+name+'"' for name in columns),
            'query':'SELECT * FROM pg_temp.catalog_manual_expected_preview_requests',
            'ignored_columns':('created_at',)},
        'preview_pages':{'key':'request_id,role','columns':'request_id,role,page_number,small_key,large_key',
            'query':'SELECT * FROM pg_temp.catalog_manual_expected_preview_pages','ignored_columns':()},
    }
    return models, preparation


def render_manual_previews(fingerprint, schema='monocorpus', dataset_schema='public', *, source_signatures):
    metadata = build_metadata(schema)
    build_metadata(dataset_schema)
    if not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
        raise ValueError('invalid reviewed fingerprint')
    if not isinstance(source_signatures, dict) or set(source_signatures)!=set(SOURCE_TABLES) or any(
        not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}',value) for value in source_signatures.values()):
        raise ValueError('invalid reviewed preview source signatures')
    models, preparations = _preview_models(schema,dataset_schema,metadata)
    verification = _verify_models(schema,models,phase='preview')
    sources = []
    for name in SOURCE_TABLES:
        owner = dataset_schema if name=='document' else schema
        sources.append(f"""IF (SELECT count(*) FROM "{owner}"."{name}") IS DISTINCT FROM
            (migration.manifest->'source_tables'->'{name}'->>'rows')::bigint THEN
            RAISE EXCEPTION 'preview source count changed for {name}'; END IF;
        IF ({preview_source_signature_query(owner,name)}) IS DISTINCT FROM '{source_signatures[name]}' THEN
            RAISE EXCEPTION 'preview source changed for {name}'; END IF;""")
    progress = f"""jsonb_build_object(
        'loaded_requests',(SELECT count(*) FROM "{schema}".catalog_preview_requests),
        'loaded_pages',(SELECT count(*) FROM "{schema}".catalog_preview_pages),
        'blocked_legacy_requests',(SELECT count(*) FROM pg_temp.catalog_manual_preview_source WHERE legacy_private))"""
    inserts = '\n'.join(f'INSERT INTO "{schema}".catalog_{name} ({model["columns"]})\n{model["query"]};'
                        for name,model in models.items())
    body = f"""DECLARE
    migration "{schema}".catalog_imports;
BEGIN
    SET LOCAL lock_timeout='10s';
    SET LOCAL work_mem='4MB';
    SET LOCAL maintenance_work_mem='16MB';
    SET LOCAL TimeZone='UTC';
    IF NOT pg_try_advisory_xact_lock(hashtext('catalog-import:{schema}')) THEN
        RAISE EXCEPTION 'another catalog migration is running'; END IF;
    SELECT * INTO migration FROM "{schema}".catalog_imports WHERE source_fingerprint='{fingerprint}' FOR UPDATE;
    IF migration.import_id IS NULL OR migration.state<>'loading'
      OR migration.manifest->'manual_editor' IS DISTINCT FROM 'true'::jsonb
      OR migration.manifest->>'evidence_mode' IS DISTINCT FROM 'essential'
      OR migration.manifest->>'dataset_schema' IS DISTINCT FROM '{dataset_schema}'
      OR jsonb_typeof(migration.manifest->'actor') IS DISTINCT FROM 'string'
      OR btrim(migration.manifest->>'actor')='' THEN
        RAISE EXCEPTION 'reviewed manual loading import and actor required'; END IF;
    LOCK TABLE {','.join('"'+schema+'"."'+table.name+'"' for table in sorted(metadata.tables.values(),key=lambda t:t.name))} IN SHARE ROW EXCLUSIVE MODE;
    LOCK TABLE "{dataset_schema}".document,"{schema}".library_book_previews IN SHARE ROW EXCLUSIVE MODE;
    IF NOT coalesce(migration.manifest->'manual_completed_steps' @> '["previews"]'::jsonb,false) AND
        migration.manifest->'manual_completed_steps' IS DISTINCT FROM '{PREVIOUS_STEPS}'::jsonb THEN
        RAISE EXCEPTION 'preview phase is out of order'; END IF;
    IF (migration.manifest->'manual_location_progress'->>'loaded_documents')::bigint IS DISTINCT FROM
        (migration.manifest->>'documents')::bigint OR
        (SELECT count(*) FROM "{schema}".catalog_locations) IS DISTINCT FROM
        (migration.manifest->'manual_location_progress'->>'loaded_locations')::bigint OR
        (SELECT count(*) FROM "{schema}".catalog_documents) IS DISTINCT FROM (migration.manifest->>'documents')::bigint OR
        (SELECT count(*) FROM "{schema}".catalog_publications) IS DISTINCT FROM (migration.manifest->>'documents')::bigint OR
        (SELECT count(*) FROM "{schema}".catalog_contributions) IS DISTINCT FROM
        (migration.manifest->'manual_credit_progress'->>'loaded_credits')::bigint OR
        (SELECT count(*) FROM "{schema}".catalog_names) IS DISTINCT FROM
        (migration.manifest->'manual_credit_progress'->>'loaded_names')::bigint THEN
        RAISE EXCEPTION 'preceding location loading is incomplete'; END IF;
    {chr(10).join(sources)}
    IF EXISTS(SELECT 1 FROM "{schema}".library_book_previews p
        LEFT JOIN "{dataset_schema}".document d USING(md5) WHERE d.md5 IS NULL) THEN
        RAISE EXCEPTION 'orphan preview checkpoint'; END IF;
    IF EXISTS(SELECT 1 FROM "{dataset_schema}".document s
        FULL JOIN "{schema}".catalog_documents t USING(md5)
        WHERE s.md5 IS NULL OR t.md5 IS NULL OR
            t.restricted IS DISTINCT FROM (s.sharing_restricted IS NOT FALSE)) THEN
        RAISE EXCEPTION 'preview privacy mapping differs'; END IF;
    {preparations}
    IF migration.manifest->'manual_completed_steps' @> '["previews"]'::jsonb THEN
        {verification}
        IF migration.manifest->'manual_preview_progress' IS DISTINCT FROM {progress} THEN
            RAISE EXCEPTION 'preview staging differs from saved progress'; END IF;
        RETURN;
    END IF;
    IF EXISTS(SELECT 1 FROM "{schema}".catalog_preview_requests) OR EXISTS(SELECT 1 FROM "{schema}".catalog_preview_pages) THEN
        RAISE EXCEPTION 'preview staging contains untracked rows'; END IF;
    {inserts}
    {verification}
    PERFORM setval(pg_get_serial_sequence('"{schema}".catalog_preview_requests','request_id'),
        coalesce((SELECT max(request_id) FROM "{schema}".catalog_preview_requests),1),
        EXISTS(SELECT 1 FROM "{schema}".catalog_preview_requests));
    UPDATE "{schema}".catalog_imports SET manifest=jsonb_set(jsonb_set(manifest,
        '{{manual_preview_progress}}',{progress}),'{{manual_completed_steps}}',
        manifest->'manual_completed_steps'||'["previews"]'::jsonb) WHERE import_id=migration.import_id;
END;"""
    return _editor_block(body)
