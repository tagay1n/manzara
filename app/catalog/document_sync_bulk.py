"""Set-based sync writes; identifiers and writable columns are backend-owned."""

import json

from sqlalchemy import text


_RECORDS = {
    'publication': ('catalog_publications', 'publication_id', ('has_metadata', 'metadata_present')),
    'document': ('catalog_documents', 'md5', ('mime_type', 'complete', 'restricted')),
    'source': ('catalog_locations', 'location_id', ('source_path', 'resource_id', 'public_url', 'public_key', 'size')),
    'primary': ('catalog_locations', 'location_id', ('locator', 'size', 'etag', 'verified_at')),
}


def _json(value):
    return json.dumps(value, default=lambda item: item.isoformat(), ensure_ascii=False)


def insert_records(conn, kind, rows):
    if not rows:
        return []
    relation, key, fields = _RECORDS[kind]
    columns = {
        'publication': (key, *fields),
        'document': (key, 'publication_id', *fields),
        'source': ('md5', 'provider', 'purpose', *fields),
    }[kind]
    returned = [dict(row) for row in conn.execute(text(f'''
        INSERT INTO {relation} ({','.join(columns)})
        SELECT {','.join('item.' + column for column in columns)}
        FROM jsonb_populate_recordset(NULL::{relation}, CAST(:rows AS JSONB)) AS item
        RETURNING *'''), {'rows': _json(rows)}).mappings()]
    if len(returned) != len(rows):
        raise RuntimeError('Sync bulk insert returned an incomplete batch')
    return returned


def update_records(conn, kind, rows):
    if not rows:
        return []
    relation, key, fields = _RECORDS[kind]
    returned = [dict(row) for row in conn.execute(text(f'''
        UPDATE {relation} AS target
        SET {','.join(column + '=item.' + column for column in fields)},
            revision=target.revision+1, updated_at=CURRENT_TIMESTAMP
        FROM jsonb_populate_recordset(NULL::{relation}, CAST(:rows AS JSONB)) AS item
        WHERE target.{key}=item.{key} AND target.revision=item.revision
        RETURNING target.*'''), {'rows': _json(rows)}).mappings()]
    if {row[key] for row in returned} != {row[key] for row in rows}:
        raise RuntimeError('Sync bulk update lost a locked record; batch rolled back')
    return returned


def reserve_publications(conn, md5s):
    """Allocate one existing sequence identity per new MD5 in one round trip."""
    if not md5s:
        return {}
    rows = conn.execute(text('''SELECT md5,
        nextval(pg_get_serial_sequence('catalog_publications','publication_id')) AS publication_id
        FROM unnest(CAST(:md5s AS TEXT[])) AS item(md5) ORDER BY md5'''), {'md5s': md5s}).mappings()
    return {row['md5']: row['publication_id'] for row in rows}
