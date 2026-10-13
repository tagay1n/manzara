"""Use tt-Latn-x-zaman in catalog facts and structured historical language fields.

Revision ID: 20261013_0068
Revises: 20261012_0067
"""

import json

from alembic import op
from sqlalchemy import text

from app.task_runtime.logging import log_message

revision = "20261013_0068"
down_revision = "20261012_0067"
branch_labels = None
depends_on = None

# Freeze the agreed spelling and field scope in this revision.
_NEW_TAG = "tt-Latn-x-zaman"
_OLD_TAGS = frozenset({"tt-latn-x-zamanalif", "tt-latn-x-zaman-alif"})
_LANGUAGE_FIELDS = frozenset({"inLanguage", "language", "languages"})
_OLD_TAG_PATTERN = r"(^|,)[[:space:]]*tt-latn-x-zaman(alif|-alif)[[:space:]]*(,|$)"


def _rewrite(value, language=False):
    """Rewrite language values, leaving unrelated prose and JSON strings intact."""
    if isinstance(value, dict):
        return {key: _rewrite(item, language or key in _LANGUAGE_FIELDS)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite(item, language) for item in value]
    if language and isinstance(value, str):
        parts = value.split(",")
        return ",".join(_NEW_TAG if part.strip().casefold() in _OLD_TAGS else part
                        for part in parts)
    return value


def _rewrite_json_columns(connection, schema):
    # Views reconstruct catalog data and must not receive duplicate writes.
    # Include JSON held as text by older retained checkpoint/template contracts.
    columns = connection.execute(text('''
        SELECT c.table_name,c.column_name,c.data_type,
            EXISTS(SELECT 1 FROM information_schema.columns r
                WHERE r.table_schema=c.table_schema AND r.table_name=c.table_name
                  AND r.column_name='revision') AS versioned,
            EXISTS(SELECT 1 FROM information_schema.columns r
                WHERE r.table_schema=c.table_schema AND r.table_name=c.table_name
                  AND r.column_name='updated_at' AND r.data_type LIKE 'timestamp%') AS timestamped
        FROM information_schema.columns c
        JOIN information_schema.tables t USING(table_catalog,table_schema,table_name)
        WHERE c.table_schema=:schema AND t.table_type='BASE TABLE'
          AND (c.data_type IN ('json','jsonb') OR
              (c.data_type='text' AND c.column_name ~ '(json|response|metadata|snapshot|inventory)'))
        ORDER BY c.table_name,c.column_name
    '''), {'schema': schema}).mappings().all()
    quote = connection.dialect.identifier_preparer.quote_identifier
    changed = 0
    for column in columns:
        table = f'{quote(schema)}.{quote(column["table_name"])}'
        field = quote(column['column_name'])
        rows = connection.execute(text(f'''
            SELECT ctid::text AS row_id,{field} AS payload FROM {table}
            WHERE lower({field}::text) LIKE '%tt-latn-x-zaman%'
            FOR UPDATE
        ''')).mappings().all()
        for row in rows:
            payload = row['payload']
            if column['data_type'] == 'text':
                try:
                    payload = json.loads(payload)
                except (ValueError, TypeError):
                    continue
            rewritten = _rewrite(payload)
            if rewritten == payload:
                continue
            data_type = column['data_type']
            assignments = [f'{field}=CAST(:payload AS {data_type})']
            if column['versioned']:
                assignments.append('revision=revision+1')
            if column['timestamped']:
                assignments.append('updated_at=CURRENT_TIMESTAMP')
            connection.execute(text(f'''UPDATE {table} SET {','.join(assignments)}
                WHERE ctid=CAST(:row_id AS tid)'''),
                {'row_id': row['row_id'], 'payload': json.dumps(rewritten, ensure_ascii=False)})
            changed += 1
    return changed


def upgrade():
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError('Language-tag migration requires online catalog inspection')
    schema = context.opts['catalog_schema']
    connection = op.get_bind()
    publications = f'"{schema}".catalog_publications'
    languages = f'"{schema}".catalog_publication_languages'
    references = f'"{schema}".catalog_references'
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    connection.execute(text(f'LOCK TABLE {publications},{languages},{references} IN SHARE ROW EXCLUSIVE MODE'))
    publication_ids = set()
    counts = {}
    for table in (languages, references):
        rows = connection.execute(text(f'''SELECT publication_id,language FROM {table}
            WHERE language ~* :pattern FOR UPDATE'''), {'pattern': _OLD_TAG_PATTERN}).mappings().all()
        counts[table] = len(rows)
        for row in rows:
            connection.execute(text(f'''UPDATE {table} SET language=:new
                WHERE publication_id=:publication_id AND language=:old'''),
                {'new': _rewrite(row['language'], True),
                 'publication_id': row['publication_id'], 'old': row['language']})
            publication_ids.add(row['publication_id'])
    for publication_id in sorted(publication_ids):
        connection.execute(text(f'''UPDATE {publications}
            SET revision=revision+1,updated_at=CURRENT_TIMESTAMP
            WHERE publication_id=:publication_id'''), {'publication_id': publication_id})
    json_changes = _rewrite_json_columns(connection, schema)
    for table, constraint in ((languages, 'ck_catalog_publication_language_zaman'),
                              (references, 'ck_catalog_reference_language_zaman')):
        connection.execute(text(f'''ALTER TABLE {table} ADD CONSTRAINT {constraint}
            CHECK (language IS NULL OR language !~* :pattern)'''), {'pattern': _OLD_TAG_PATTERN})
    log_message(f'Zamanalif language migration: {sum(counts.values())} language rows, '
                f'{len(publication_ids)} publications, {json_changes} structured JSON values updated')


def downgrade():
    raise RuntimeError('Restoring former Zamanalif language tags requires explicit owner review')
