"""Offline, guarded deferral of reviewed performance indexes during import."""

import re

from app.catalog.schema import build_metadata
from scripts.catalog_sql import _editor_block


def render_performance_index_cleanup(fingerprint, schema='monocorpus'):
    return _render_cleanup(fingerprint, schema, (
        ('library_collection_document_features','idx_library_collection_features_core_trgm','gin','title_core gin_trgm_ops'),
        ('library_collection_document_features','idx_library_collection_features_eligible_core','btree','eligible, title_core'),
    ), {'library_collection_document_features':
        "(migration.manifest->'source_tables'->'library_collection_document_features'->>'rows')::bigint"})


def render_additional_performance_index_cleanup(fingerprint, schema='monocorpus'):
    specs = (
        ('library_book_previews','idx_library_book_previews_status','btree','recipe_version, status, updated_at, md5'),
        ('library_non_pdf_extraction_state','idx_library_non_pdf_extraction_queue','btree','extractor_version, status, updated_at, md5'),
        ('library_metadata_quality_state','idx_library_metadata_quality_state_status','btree','status, contract_version, updated_at'),
        ('catalog_contributions','idx_catalog_contributions_name_role','btree','name_id, role'),
        ('catalog_contributions','idx_catalog_contributions_publication','btree','publication_id'),
        ('catalog_contributions','idx_catalog_contributions_entity','btree','entity_id'),
        ('catalog_documents','idx_catalog_documents_publication','btree','publication_id'),
        ('catalog_preview_requests','idx_catalog_preview_queue','btree','status, request_id'),
    )
    counts = {name:f"(migration.manifest->'source_tables'->'{name}'->>'rows')::bigint"
              for name in ('library_book_previews','library_non_pdf_extraction_state','library_metadata_quality_state')}
    counts.update({
        'catalog_contributions':"(migration.manifest->'manual_credit_progress'->>'loaded_credits')::bigint",
        'catalog_documents':"(migration.manifest->>'documents')::bigint",
        'catalog_preview_requests':"(migration.manifest->'manual_preview_progress'->>'loaded_requests')::bigint",
    })
    phase = """IF NOT coalesce(migration.manifest->'manual_completed_steps' @>
        '["initialized","foundation","taxonomy","aliases","documents","metadata_details","credits","locations","previews"]'::jsonb,false) THEN
        RAISE EXCEPTION 'completed previews required before additional cleanup'; END IF;"""
    return _render_cleanup(fingerprint, schema, specs, counts, phase)


def _render_cleanup(fingerprint, schema, specs, counts, phase=''):
    build_metadata(schema)
    if not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
        raise ValueError('invalid reviewed fingerprint')
    locks = ','.join(f'"{schema}"."{table}"' for table in sorted(counts))
    checks = '\n'.join(f"""IF (SELECT count(*) FROM "{schema}"."{table}") IS DISTINCT FROM {expected} THEN
        RAISE EXCEPTION 'cleanup row count changed for {table}'; END IF;""" for table,expected in counts.items())
    values = ',\n        '.join('('+','.join("'"+value+"'" for value in spec)+')' for spec in specs)
    body = f"""DECLARE
    migration "{schema}".catalog_imports;
    spec record;
    actual record;
    remembered jsonb;
    deferred jsonb;
    expected_definition text;
BEGIN
    SET LOCAL search_path=pg_catalog,public;
    SET LOCAL lock_timeout='10s';
    SET LOCAL statement_timeout='29s';
    IF pg_is_in_recovery() THEN
        RAISE EXCEPTION 'cleanup requires the primary database'; END IF;
    IF NOT pg_try_advisory_xact_lock(hashtext('catalog-import:{schema}')) THEN
        RAISE EXCEPTION 'another catalog migration is running'; END IF;
    SELECT * INTO migration FROM "{schema}".catalog_imports
        WHERE source_fingerprint='{fingerprint}' FOR UPDATE;
    IF migration.import_id IS NULL OR migration.state<>'loading'
      OR migration.manifest->'manual_editor' IS DISTINCT FROM 'true'::jsonb
      OR migration.manifest->>'evidence_mode' IS DISTINCT FROM 'essential' THEN
        RAISE EXCEPTION 'reviewed manual loading import required'; END IF;
    {phase}
    LOCK TABLE {locks} IN ACCESS EXCLUSIVE MODE;
    {checks}
    deferred:=coalesce(migration.manifest->'manual_deferred_performance_indexes','[]'::jsonb);
    IF jsonb_typeof(deferred)<>'array' THEN
        RAISE EXCEPTION 'invalid saved index restoration manifest'; END IF;
    FOR spec IN SELECT * FROM (VALUES
        {values}
    ) AS reviewed(table_name,name,method,columns) LOOP
        expected_definition:='CREATE INDEX '||quote_ident(spec.name)||' ON '||quote_ident('{schema}')||
            '.'||quote_ident(spec.table_name)||' USING '||spec.method||' ('||spec.columns||')';
        SELECT value INTO remembered FROM jsonb_array_elements(deferred)
            WHERE value->>'name'=spec.name;
        IF remembered IS NOT NULL AND (
            remembered->>'schema' IS DISTINCT FROM '{schema}' OR
            remembered->>'table' IS DISTINCT FROM spec.table_name OR
            remembered->>'definition' IS DISTINCT FROM expected_definition) THEN
            RAISE EXCEPTION 'saved index restoration definition changed'; END IF;
        SELECT x.*,pg_get_indexdef(x.indexrelid) AS definition,
            pg_relation_size(x.indexrelid) AS bytes INTO actual FROM pg_index x
            WHERE x.indexrelid=to_regclass(quote_ident('{schema}')||'.'||quote_ident(spec.name));
        IF NOT FOUND THEN
            IF remembered IS NULL THEN
                RAISE EXCEPTION 'missing index has no saved restoration definition'; END IF;
            CONTINUE;
        END IF;
        IF actual.definition IS DISTINCT FROM expected_definition OR
            actual.indrelid<>to_regclass(quote_ident('{schema}')||'.'||quote_ident(spec.table_name)) OR
            actual.indisunique OR actual.indisprimary OR actual.indisexclusion OR
            actual.indisreplident OR NOT actual.indisvalid OR NOT actual.indisready OR
            EXISTS(SELECT 1 FROM pg_constraint WHERE conindid=actual.indexrelid) THEN
            RAISE EXCEPTION 'index definition changed or enforces data integrity'; END IF;
        IF remembered IS NULL THEN
            deferred:=deferred||jsonb_build_array(jsonb_build_object(
                'schema','{schema}','table',spec.table_name,
                'name',spec.name,'definition',actual.definition,'bytes',actual.bytes));
        END IF;
        EXECUTE 'DROP INDEX '||quote_ident('{schema}')||'.'||quote_ident(spec.name)||' RESTRICT';
    END LOOP;
    UPDATE "{schema}".catalog_imports SET manifest=jsonb_set(manifest,
        '{{manual_deferred_performance_indexes}}',deferred) WHERE import_id=migration.import_id;
END;"""
    return _editor_block(body)
