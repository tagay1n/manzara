"""Render guarded offline catalog SQL; this module never connects to a database."""

import argparse
import json
import re

from app.catalog.schema import build_metadata
from app.catalog.importer import SOURCE_TABLES


def _editor_block(body):
    # Manual import stages describe the frozen pre-retirement source schema.
    body = body.replace("BEGIN", """BEGIN
    IF EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='personality_normalization_checkpoints'
              AND column_name='decision_reason') THEN
        RAISE EXCEPTION 'manual source import is retired; use the recorded PostgreSQL recovery dump';
    END IF;""", 1)
    # PG Studio splits the raw text at semicolons, including inside quoted blocks.
    # PostgreSQL decodes octal escapes in E-strings before parsing the block body.
    encoded = body.replace('\\', '\\\\').replace("'", "''").replace(';', r'\073')
    return "DO E'" + encoded + "';\n"


def render_manual_initialize(fingerprint, manifest, schema="monocorpus", dataset_schema="public"):
    """Record a reviewed, paused migration without activating any task adapters."""
    tables = sorted(table.name for table in build_metadata(schema).tables.values())
    build_metadata(dataset_schema)
    if not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
        raise ValueError('invalid reviewed fingerprint')
    if manifest.get('evidence_mode') != 'essential' or manifest.get('manual_editor') is not True:
        raise ValueError('manual initialization requires essential evidence policy')
    source_tables = manifest['source_tables']
    required = {'document', 'metadata', 'classification', 'normalization_canonicals',
                'normalization_aliases', 'library_collections', 'library_collection_items'}
    if set(source_tables) - set(SOURCE_TABLES) or not required <= set(source_tables):
        raise ValueError('unknown or missing source table')
    report = {**manifest, 'dataset_schema': dataset_schema, 'manual_completed_steps': ['initialized']}
    payload = json.dumps(report, ensure_ascii=True).replace("'", "''")
    relations = ','.join(f'"{schema}"."{name}"' for name in tables)
    source_relations, checks = [], []
    for name, info in source_tables.items():
        count = info['rows']
        if type(count) is not int or count < 0:
            raise ValueError('source row counts must be nonnegative integers')
        if not re.fullmatch(r'[0-9a-f]{64}', info['fingerprint']):
            raise ValueError('invalid source table fingerprint')
        owner = dataset_schema if name in {'document', 'metadata', 'classification', 'isbn_keep_many'} else schema
        relation = f'"{owner}"."{name}"'
        source_relations.append(relation)
        checks.append(f"""IF (SELECT count(*) FROM {relation}) <> {count} THEN
    RAISE EXCEPTION 'source count changed for {name}';
END IF;""")
    occupied = '\nUNION ALL '.join(f'SELECT 1 FROM "{schema}"."{name}"' for name in tables)
    body = f"""DECLARE
    previous "{schema}".catalog_imports;
    expected JSONB := '{payload}'::jsonb;
BEGIN
    PERFORM set_config('lock_timeout', '10s', true);
    IF NOT pg_try_advisory_xact_lock(hashtext('catalog-import:{schema}')) THEN
        RAISE EXCEPTION 'another catalog migration is running';
    END IF;
    SELECT * INTO previous FROM "{schema}".catalog_imports
      WHERE source_fingerprint='{fingerprint}' FOR UPDATE;
    IF previous.import_id IS NOT NULL AND previous.state='loading' AND previous.manifest @> expected THEN
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM "{schema}".catalog_imports) THEN
        RAISE EXCEPTION 'another or completed import exists';
    END IF;
    LOCK TABLE {relations} IN ACCESS EXCLUSIVE MODE;
    IF EXISTS ({occupied}) THEN
        RAISE EXCEPTION 'catalog staging contains rows';
    END IF;
    LOCK TABLE {','.join(source_relations)} IN SHARE ROW EXCLUSIVE MODE;
    {chr(10).join(checks)}
    INSERT INTO "{schema}".catalog_imports(source_fingerprint,manifest,state)
      VALUES ('{fingerprint}',expected,'loading');
END;"""
    return _editor_block(body)


def render_empty_staging_reset(schema="monocorpus"):
    """Guarded cleanup for clients that preserve complete PL/pgSQL statements."""
    tables = sorted(table.name for table in build_metadata(schema).tables.values())
    relations = ",\n        ".join(f'"{schema}"."{name}"' for name in tables)
    checks = "\n        UNION ALL ".join(f'SELECT 1 FROM "{schema}"."{name}"' for name in tables)
    return f"""DO $catalog_cleanup$
BEGIN
    PERFORM set_config('lock_timeout', '10s', true);
    IF NOT pg_try_advisory_xact_lock(hashtext('catalog-import:{schema}')) THEN
        RAISE EXCEPTION 'Another catalog migration is running; cleanup refused';
    END IF;
    LOCK TABLE
        {relations}
      IN ACCESS EXCLUSIVE MODE;
    IF EXISTS (
        {checks}
    ) THEN
        RAISE EXCEPTION 'Catalog staging contains rows; cleanup refused';
    END IF;
    TRUNCATE TABLE
        {relations}
      RESTART IDENTITY RESTRICT;
END
$catalog_cleanup$;
"""


def render_editor_buffer(schema="monocorpus", dataset_schema="public"):
    """Separate precheck, cleanup, and verification for semicolon-splitting editors."""
    tables = sorted(table.name for table in build_metadata(schema).tables.values())
    build_metadata(dataset_schema)  # Validate the dataset identifier as well.
    counts = ",\n        ".join(
        f"'{name.removeprefix('catalog_')}', (SELECT count(*) FROM \"{schema}\".\"{name}\")"
        for name in tables)
    check = f"""WITH staging AS (
    SELECT jsonb_build_object(
        {counts}
    ) AS staging_rows
)
SELECT jsonb_pretty(jsonb_build_object(
    'database_name', current_database(),
    'database_bytes', pg_database_size(current_database()),
    'database_size', pg_size_pretty(pg_database_size(current_database())),
    'original_documents', (SELECT count(*) FROM "{dataset_schema}".document),
    'original_metadata', (SELECT count(*) FROM "{dataset_schema}".metadata),
    'can_cleanup', NOT EXISTS (
        SELECT 1 FROM jsonb_each(staging_rows) WHERE value::TEXT::BIGINT <> 0
    ),
    'staging_rows', staging_rows
)) AS result FROM staging;"""
    relations = ",\n    ".join(f'"{schema}"."{name}"' for name in tables)
    return f"""-- TEMPORARY MANUAL COMMAND BUFFER. Copy one numbered statement at a time.
-- Keep every catalog writer paused throughout this procedure. Stop on any error.
-- These statements only reclaim empty catalog staging left by rolled-back imports.
-- This file does not perform the data import or retire original tables.

-- 1. READ-ONLY PRECHECK
-- Proceed only if can_cleanup=true and the original counts match our review.
-- Production expectations: original_documents=70878, original_metadata=69661.
{check}

-- 2. CLEANUP: run only after statement 1 passes, with writers still paused.
-- This statement has no inline empty-table guard. Never use it on populated staging.
-- RESTRICT refuses dependencies outside this explicit table list. No CASCADE.
TRUNCATE TABLE
    {relations}
RESTART IDENTITY RESTRICT;

-- 3. READ-ONLY VERIFICATION: paste this JSON result in our conversation.
-- Every staging count must still be zero and original counts must be unchanged.
{check}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", default="monocorpus")
    parser.add_argument("--dataset-schema", default="public")
    parser.add_argument("--editor-buffer", action="store_true")
    args = parser.parse_args()
    statement = (render_editor_buffer(args.schema, args.dataset_schema) if args.editor_buffer
                 else render_empty_staging_reset(args.schema))
    print(statement, end="")


if __name__ == "__main__":
    main()
