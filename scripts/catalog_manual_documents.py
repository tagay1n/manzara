"""Offline SQL rendering for bounded, restartable publication/document loading."""

import re

from app.catalog.metadata import SCALARS
from app.catalog.schema import build_metadata
from app.catalog.schema_org import ACCESS_MODES, SUPPORTED_TYPES
from scripts.catalog_manual_foundation import _verify_models
from scripts.catalog_sql import _editor_block


SOURCE_TABLES = ('document', 'metadata', 'library_collection_items')
PREVIOUS_STEPS = '["initialized","foundation","taxonomy","aliases"]'
MAX_BATCH_ROWS = 10000


def _md5(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('invalid document boundary')
    return "'" + value + "'"


def _range(lower_md5, upper_md5, alias='c'):
    upper = _md5(upper_md5)
    return f'{alias}.md5<={upper}' if lower_md5 is None else f'{alias}.md5>{_md5(lower_md5)} AND {alias}.md5<={upper}'


def document_source_signature_query(schema, table, lower_md5, upper_md5):
    build_metadata(schema)
    if table not in SOURCE_TABLES:
        raise ValueError('unsupported document source table')
    # Hash each row before aggregation, avoiding a full-corpus JSON allocation.
    return f"""SELECT encode(sha256(convert_to(coalesce(string_agg(
        encode(sha256(convert_to(to_jsonb(c)::text,'UTF8')),'hex'),'' ORDER BY md5),''),'UTF8')),'hex')
        FROM "{schema}"."{table}" c WHERE {_range(lower_md5, upper_md5)}"""


def _items(expression):
    return f"""CASE WHEN {expression} IS NULL OR {expression}='null'::jsonb THEN '[]'::jsonb
        WHEN jsonb_typeof({expression})='array' THEN {expression} ELSE jsonb_build_array({expression}) END"""


def _languages(expression):
    whitespace = list(range(9, 14)) + list(range(28, 33)) + [133, 160, 5760] + list(range(8192, 8203)) + [8232, 8233, 8239, 8287, 12288]
    trim = 'concat(' + ','.join(f'chr({code})' for code in whitespace) + ')'
    return f"""ARRAY(SELECT cleaned FROM (
        SELECT btrim(value,{trim}) AS cleaned,position
        FROM unnest(string_to_array(coalesce({expression},''),',')) WITH ORDINALITY AS part(value,position)
    ) language WHERE cleaned<>'' ORDER BY position)"""


def _document_models(schema, metadata, lower_md5, upper_md5, offset, rows):
    fields = {
        'publications': {
            'publication_id': 's.publication_id',
            **{column: "CASE WHEN s.payload IS NULL THEN 'CreativeWork' ELSE s.payload->>'@type' END" if column == 'work_type'
               else "(s.payload->>'numberOfPages')::integer" if column == 'page_count' else f"s.payload->>'{key}'"
               for key, column in SCALARS.items()},
            'languages': _languages("CASE WHEN s.payload IS NULL THEN s.language ELSE s.payload->>'inLanguage' END"),
            'audience_array': "coalesce(jsonb_typeof(s.payload->'audience')='array',false)",
            'inclusion': "CASE s.lib WHEN true THEN 'included' WHEN false THEN 'excluded' ELSE 'pending' END",
            'evaluation_method': 's.lib_eval_method', 'classification_id': 's.classification_id',
            'collection_id': 's.collection_id', 'collection_item_title': 's.item_title',
            'collection_created_at': 's.item_created_at', 'collection_updated_at': 's.item_updated_at',
            'has_metadata': 's.has_metadata', 'metadata_present': 's.payload IS NOT NULL', 'merged_into_id': 'NULL::bigint',
        },
        'documents': {
            'md5': 's.md5', 'publication_id': 's.publication_id', 'mime_type': 's.mime_type',
            'complete': 's."full" IS TRUE', 'restricted': 's.sharing_restricted IS NOT FALSE', 'selected': 'true',
            'content_extraction_method': 's.content_extraction_method', 'meta_extraction_method': 's.meta_extraction_method',
            'access_modes': f"ARRAY(SELECT value FROM jsonb_array_elements_text({_items('s.payload->\'accessMode\'')}) WITH ORDINALITY AS mode(value,position) ORDER BY position)",
        },
    }
    models, preparations = {}, []
    for name, expressions in fields.items():
        columns = [column.name for column in metadata.tables[f'{schema}.catalog_{name}'].columns
                   if column.name not in {'revision', 'updated_at'}]
        projection = ','.join(f'{expressions[column]} AS "{column}"' for column in columns)
        expected = 'catalog_manual_expected_' + name
        preparations.append(f'CREATE TEMP TABLE {expected} ON COMMIT DROP AS SELECT {projection} FROM pg_temp.catalog_manual_document_source s;')
        predicate = _range(lower_md5, upper_md5, 't') if name == 'documents' else f't.publication_id>{offset} AND t.publication_id<={offset + rows}'
        models[name] = {'columns': ','.join(f'"{column}"' for column in columns),
                        'key': 'md5' if name == 'documents' else 'publication_id',
                        'query': f'SELECT * FROM pg_temp.{expected}',
                        'target_query': f'SELECT * FROM "{schema}".catalog_{name} t WHERE {predicate}'}
    return models, '\n'.join(preparations)


def _scalar_guard():
    checks = [f"(payload?'{key}' AND jsonb_typeof(payload->'{key}') IS DISTINCT FROM 'string')"
              for key in SCALARS if key != 'numberOfPages']
    types = ','.join("'" + value + "'" for value in sorted(SUPPORTED_TYPES))
    modes = ','.join("'" + value + "'" for value in sorted(ACCESS_MODES))
    checks.extend([
        "jsonb_typeof(payload) IS DISTINCT FROM 'object'",
        f"coalesce(payload->>'@type','') NOT IN ({types})",
        "(payload?'@context' AND payload->>'@context' IS DISTINCT FROM 'https://schema.org')",
        "(payload?'numberOfPages' AND (jsonb_typeof(payload->'numberOfPages') IS DISTINCT FROM 'number' OR (payload->>'numberOfPages')!~'^[1-9][0-9]*$'))",
        "(payload->>'inLanguage' IS NOT NULL AND jsonb_typeof(payload->'inLanguage') IS DISTINCT FROM 'string')",
        f"EXISTS(SELECT 1 FROM jsonb_array_elements({_items('payload->\'accessMode\'')}) mode WHERE jsonb_typeof(mode) IS DISTINCT FROM 'string' OR mode#>>'{{}}' NOT IN ({modes}))",
    ])
    return "IF EXISTS(SELECT 1 FROM pg_temp.catalog_manual_document_source WHERE payload IS NOT NULL AND (" + '\n OR '.join(checks) + ")) THEN RAISE EXCEPTION 'invalid bibliographic scalar or access mode'; END IF;"


def _validate_document_batch(fingerprint, lower_md5, upper_md5, offset, rows, final_batch, source_signatures):
    if not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
        raise ValueError('invalid reviewed fingerprint')
    if type(offset) is not int or offset < 0 or type(rows) is not int or not 1 <= rows <= MAX_BATCH_ROWS or type(final_batch) is not bool:
        raise ValueError('invalid document batch sizes or final flag')
    _md5(upper_md5)
    if lower_md5 is not None:
        _md5(lower_md5)
        if lower_md5 >= upper_md5:
            raise ValueError('invalid document boundary order')
    if (offset == 0) != (lower_md5 is None):
        raise ValueError('document offset and lower boundary disagree')
    if not isinstance(source_signatures, dict) or set(source_signatures) != set(SOURCE_TABLES) or any(
            not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for value in source_signatures.values()):
        raise ValueError('invalid reviewed document source signatures')


def _document_source_preparation(schema, dataset_schema, lower_md5, upper_md5, offset, rows):
    return f"""CREATE TEMP TABLE catalog_manual_document_source ON COMMIT DROP AS
      SELECT d.md5,d.mime_type,d.language,d."full",d.sharing_restricted,d.content_extraction_method,d.meta_extraction_method,
        {offset}+row_number() OVER(ORDER BY d.md5) AS publication_id,
        m.md5 IS NOT NULL AS has_metadata,nullif(m.schema_org::jsonb,'null'::jsonb) AS payload,m.lib,m.lib_eval_method,m.classification_id,
        i.collection_id,i.item_title,i.created_at AS item_created_at,i.updated_at AS item_updated_at
      FROM "{dataset_schema}".document d LEFT JOIN "{dataset_schema}".metadata m USING(md5)
      LEFT JOIN "{schema}".library_collection_items i USING(md5) WHERE {_range(lower_md5, upper_md5, 'd')};
    IF (SELECT count(*) FROM pg_temp.catalog_manual_document_source)<>{rows} THEN
        RAISE EXCEPTION 'document batch has unexpected row count'; END IF;
    {_scalar_guard()}"""


def render_manual_documents(fingerprint, schema='monocorpus', dataset_schema='public', *, lower_md5, upper_md5,
                            offset, rows, final_batch, source_signatures):
    metadata = build_metadata(schema)
    build_metadata(dataset_schema)
    _validate_document_batch(fingerprint, lower_md5, upper_md5, offset, rows, final_batch, source_signatures)
    models, preparations = _document_models(schema, metadata, lower_md5, upper_md5, offset, rows)
    verification = _verify_models(schema, models, phase='document')
    tables = sorted(table.name for table in metadata.tables.values())
    owners = {name: schema if name == 'library_collection_items' else dataset_schema for name in SOURCE_TABLES}
    signature_checks = '\n'.join(f"""IF ({document_source_signature_query(owners[name], name, lower_md5, upper_md5)}) IS DISTINCT FROM '{source_signatures[name]}' THEN
        RAISE EXCEPTION 'document batch source changed for {name}'; END IF;""" for name in SOURCE_TABLES)
    count_checks = '\n'.join(f"""IF (SELECT count(*) FROM "{owners[name]}"."{name}") IS DISTINCT FROM
        (migration.manifest->'source_tables'->'{name}'->>'rows')::bigint THEN
        RAISE EXCEPTION 'document source count changed for {name}'; END IF;""" for name in SOURCE_TABLES)
    occupied = '\nUNION ALL '.join(f'SELECT 1 FROM "{schema}"."{name}"' for name in tables if name not in {
        'catalog_imports', 'catalog_collections', 'catalog_entities', 'catalog_entity_roles', 'catalog_names',
        'catalog_aliases', 'catalog_alias_reviews', 'catalog_classification_nodes', 'catalog_classifications'})
    inserts = '\n'.join(f'INSERT INTO "{schema}".catalog_{name} ({model["columns"]}) {model["query"]};'
                        for name, model in models.items())
    previous = 'NULL' if lower_md5 is None else _md5(lower_md5)
    final_guard = f"""IF {offset + rows}<>(migration.manifest->>'documents')::bigint OR EXISTS(
        SELECT 1 FROM "{dataset_schema}".document WHERE md5>{_md5(upper_md5)}) THEN
        RAISE EXCEPTION 'last document batch does not complete reviewed corpus'; END IF;""" if final_batch else f"""IF {offset + rows}>=(migration.manifest->>'documents')::bigint THEN
        RAISE EXCEPTION 'document completion requires final batch flag'; END IF;"""
    completion = f"""UPDATE "{schema}".catalog_imports SET manifest=jsonb_set(manifest,'{{manual_completed_steps}}',
        manifest->'manual_completed_steps'||'["documents"]'::jsonb) WHERE import_id=migration.import_id;""" if final_batch else ''
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
    loaded:=coalesce((migration.manifest->'manual_document_progress'->>'loaded_documents')::bigint,0);
    previous_md5:=migration.manifest->'manual_document_progress'->>'last_md5';
    IF loaded<{offset + rows} AND (loaded<>{offset} OR previous_md5 IS DISTINCT FROM {previous}
      OR migration.manifest->'manual_completed_steps' IS DISTINCT FROM '{PREVIOUS_STEPS}'::jsonb) THEN
        RAISE EXCEPTION 'document batch is out of order'; END IF;
    IF NOT migration.manifest->'manual_completed_steps' @> '["aliases"]'::jsonb THEN
        RAISE EXCEPTION 'document batch is out of order'; END IF;
    IF (SELECT count(*) FROM "{schema}".catalog_documents)<>loaded OR
      (SELECT count(*) FROM "{schema}".catalog_publications)<>loaded THEN
        RAISE EXCEPTION 'document staging differs from saved progress'; END IF;
    {count_checks}
    {signature_checks}
    {final_guard}
    {_document_source_preparation(schema, dataset_schema, lower_md5, upper_md5, offset, rows)}
    {preparations}
    IF loaded>={offset + rows} THEN
        {verification}
        RETURN;
    END IF;
    IF loaded=0 AND EXISTS({occupied}) THEN
        RAISE EXCEPTION 'unexpected staging rows before documents'; END IF;
    DROP INDEX IF EXISTS "{schema}".idx_catalog_publications_name_trgm RESTRICT;
    DROP INDEX IF EXISTS "{schema}".idx_catalog_publications_description_trgm RESTRICT;
    {inserts}
    {verification}
    PERFORM setval(pg_get_serial_sequence('"{schema}".catalog_publications','publication_id'),{offset + rows},true);
    UPDATE "{schema}".catalog_imports SET manifest=jsonb_set(manifest,'{{manual_document_progress}}',
        jsonb_build_object('loaded_documents',{offset + rows},'last_md5',{_md5(upper_md5)})) WHERE import_id=migration.import_id;
    {completion}
END;"""
    return _editor_block(body)
