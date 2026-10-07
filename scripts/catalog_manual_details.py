"""Offline rendering of bounded descriptive metadata relation batches."""

from app.catalog.schema import build_metadata
from scripts.catalog_manual_documents import (
    SOURCE_TABLES, _document_models, _document_source_preparation, _items,
    _md5, _range, _validate_document_batch, document_source_signature_query,
)
from scripts.catalog_manual_foundation import _verify_models
from scripts.catalog_sql import _editor_block


PREVIOUS_STEPS = '["initialized","foundation","taxonomy","aliases","documents"]'
DETAIL_KEYS = {
    'identifiers': 'publication_id,kind,position', 'genres': 'publication_id,position',
    'subjects': 'publication_id,position', 'audiences': 'publication_id,position',
    'sufficient_modes': 'md5,position', 'references': 'publication_id',
    'reference_authors': 'publication_id,position',
}


def _detail_queries():
    source = 'pg_temp.catalog_manual_document_source s'

    def expanded(key):
        return f"{source} CROSS JOIN LATERAL jsonb_array_elements({_items('s.payload->' + repr(key))}) WITH ORDINALITY AS item(value,ordinal)"

    ref = "s.payload->'isBasedOn'"
    return {
        'identifiers': f"""SELECT s.publication_id,'isbn'::text AS kind,value#>>'{{}}' AS value,
            regexp_replace(upper(value#>>'{{}}'),'[^0-9X]','','g') AS normalized,(ordinal-1)::integer AS position FROM {expanded('isbn')}""",
        'genres': f"""SELECT s.publication_id,value#>>'{{}}' AS value,(ordinal-1)::integer AS position FROM {expanded('genre')}""",
        'subjects': f"""SELECT s.publication_id,value->>'name' AS name,value->>'termCode' AS term_code,
            CASE WHEN jsonb_typeof(value->'inDefinedTermSet')='object' THEN value->'inDefinedTermSet'->>'name' END AS set_name,
            CASE WHEN jsonb_typeof(value->'inDefinedTermSet')='string' THEN value->>'inDefinedTermSet' ELSE value->'inDefinedTermSet'->>'url' END AS set_url,
            jsonb_typeof(value->'inDefinedTermSet')='string' AS set_is_url,
            (row_number() OVER(PARTITION BY s.publication_id ORDER BY ordinal)-1)::integer AS position FROM {expanded('about')}
            WHERE NOT (s.classification_id IS NOT NULL AND jsonb_typeof(value->'inDefinedTermSet')='object'
                AND lower(coalesce(value->'inDefinedTermSet'->>'name','')) IN ('ddc','categorypath'))""",
        'audiences': f"""SELECT s.publication_id,value->>'@type' AS kind,value->>'audienceType' AS audience_type,
            (value->>'suggestedMinAge')::integer AS min_age,(value->>'suggestedMaxAge')::integer AS max_age,
            (ordinal-1)::integer AS position FROM {expanded('audience')}""",
        'sufficient_modes': f"""SELECT s.md5,ARRAY(SELECT element FROM jsonb_array_elements_text(value->'itemListElement')
            WITH ORDINALITY AS mode(element,mode_position) ORDER BY mode_position) AS modes,
            (ordinal-1)::integer AS position FROM {expanded('accessModeSufficient')}""",
        'references': f"""SELECT s.publication_id,{ref}->>'@type' AS work_type,{ref}->>'name' AS name,
            {ref}->>'inLanguage' AS language,CASE WHEN {ref}->'url' IS NULL OR {ref}->'url'='null'::jsonb THEN NULL::text[]
                ELSE ARRAY(SELECT value FROM jsonb_array_elements_text({ref}->'url') WITH ORDINALITY AS url(value,position) ORDER BY position) END AS urls
            FROM {source} WHERE {ref} IS NOT NULL AND {ref}<>'null'::jsonb""",
        'reference_authors': f"""SELECT s.publication_id,value->>'@type' AS kind,value->>'name' AS name,
            (ordinal-1)::integer AS position FROM {source}
            CROSS JOIN LATERAL jsonb_array_elements({_items(ref + '->\'author\'')}) WITH ORDINALITY AS author(value,ordinal)""",
    }


def _detail_models(schema, metadata, lower_md5, upper_md5, offset, rows):
    models, preparations = {}, []
    for name, query in _detail_queries().items():
        columns = ','.join(f'"{column.name}"' for column in metadata.tables[f'{schema}.catalog_{name}'].columns)
        expected = 'catalog_manual_expected_' + name
        preparations.append(f'CREATE TEMP TABLE {expected} ON COMMIT DROP AS {query};')
        predicate = _range(lower_md5, upper_md5, 't') if name == 'sufficient_modes' else f't.publication_id>{offset} AND t.publication_id<={offset + rows}'
        models[name] = {'columns': columns, 'key': DETAIL_KEYS[name], 'query': f'SELECT {columns} FROM pg_temp.{expected}',
                        'target_query': f'SELECT * FROM "{schema}".catalog_{name} t WHERE {predicate}'}
    return models, '\n'.join(preparations)


def render_manual_details(fingerprint, schema='monocorpus', dataset_schema='public', *, lower_md5, upper_md5,
                          offset, rows, final_batch, source_signatures):
    metadata = build_metadata(schema)
    build_metadata(dataset_schema)
    _validate_document_batch(fingerprint, lower_md5, upper_md5, offset, rows, final_batch, source_signatures)
    models, preparations = _detail_models(schema, metadata, lower_md5, upper_md5, offset, rows)
    core, core_preparations = _document_models(schema, metadata, lower_md5, upper_md5, offset, rows)
    verification = _verify_models(schema, models, phase='metadata detail')
    core_verification = _verify_models(schema, core, phase='metadata core')
    owners = {name: schema if name == 'library_collection_items' else dataset_schema for name in SOURCE_TABLES}
    source_checks = '\n'.join(f"""IF (SELECT count(*) FROM "{owners[name]}"."{name}") IS DISTINCT FROM
        (migration.manifest->'source_tables'->'{name}'->>'rows')::bigint THEN
        RAISE EXCEPTION 'document source count changed for {name}'; END IF;
      IF ({document_source_signature_query(owners[name], name, lower_md5, upper_md5)}) IS DISTINCT FROM '{source_signatures[name]}' THEN
        RAISE EXCEPTION 'document batch source changed for {name}'; END IF;""" for name in SOURCE_TABLES)
    tables = sorted(table.name for table in metadata.tables.values())
    occupied = '\nUNION ALL '.join(f'SELECT 1 FROM "{schema}".catalog_{name}' for name in DETAIL_KEYS)
    inserts = '\n'.join(f'INSERT INTO "{schema}".catalog_{name} ({model["columns"]}) {model["query"]};'
                        for name, model in models.items())
    previous = 'NULL' if lower_md5 is None else _md5(lower_md5)
    final_guard = f"""IF {offset + rows}<>(migration.manifest->>'documents')::bigint OR EXISTS(
        SELECT 1 FROM "{dataset_schema}".document WHERE md5>{_md5(upper_md5)}) THEN
        RAISE EXCEPTION 'last metadata detail batch does not complete reviewed corpus'; END IF;""" if final_batch else f"""IF {offset + rows}>=(migration.manifest->>'documents')::bigint THEN
        RAISE EXCEPTION 'metadata detail completion requires final batch flag'; END IF;"""
    completion = f"""UPDATE "{schema}".catalog_imports SET manifest=jsonb_set(manifest,'{{manual_completed_steps}}',
        manifest->'manual_completed_steps'||'["metadata_details"]'::jsonb) WHERE import_id=migration.import_id;""" if final_batch else ''
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
    LOCK TABLE {','.join(f'"{schema}"."{name}"' for name in tables)} IN SHARE ROW EXCLUSIVE MODE;
    LOCK TABLE {','.join(f'"{owners[name]}"."{name}"' for name in SOURCE_TABLES)} IN SHARE ROW EXCLUSIVE MODE;
    loaded:=coalesce((migration.manifest->'manual_details_progress'->>'loaded_documents')::bigint,0);
    previous_md5:=migration.manifest->'manual_details_progress'->>'last_md5';
    IF loaded<{offset + rows} AND (loaded<>{offset} OR previous_md5 IS DISTINCT FROM {previous}
      OR migration.manifest->'manual_completed_steps' IS DISTINCT FROM '{PREVIOUS_STEPS}'::jsonb) THEN
        RAISE EXCEPTION 'metadata detail batch is out of order'; END IF;
    IF NOT migration.manifest->'manual_completed_steps' @> '["documents"]'::jsonb THEN
        RAISE EXCEPTION 'metadata detail batch is out of order'; END IF;
    IF (migration.manifest->'manual_document_progress'->>'loaded_documents')::bigint IS DISTINCT FROM (migration.manifest->>'documents')::bigint
      OR (SELECT count(*) FROM "{schema}".catalog_documents)<>(migration.manifest->>'documents')::bigint
      OR (SELECT count(*) FROM "{schema}".catalog_publications)<>(migration.manifest->>'documents')::bigint THEN
        RAISE EXCEPTION 'core document loading is incomplete'; END IF;
    {source_checks}
    {final_guard}
    {_document_source_preparation(schema, dataset_schema, lower_md5, upper_md5, offset, rows)}
    {core_preparations}
    {core_verification}
    {preparations}
    IF loaded>={offset + rows} THEN
        {verification}
        RETURN;
    END IF;
    IF loaded=0 AND EXISTS({occupied}) THEN
        RAISE EXCEPTION 'unexpected metadata detail staging rows'; END IF;
    {inserts}
    {verification}
    UPDATE "{schema}".catalog_imports SET manifest=jsonb_set(manifest,'{{manual_details_progress}}',
        jsonb_build_object('loaded_documents',{offset + rows},'last_md5',{_md5(upper_md5)})) WHERE import_id=migration.import_id;
    {completion}
END;"""
    return _editor_block(body)
