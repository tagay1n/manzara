"""Render bounded evidence batches reviewed against the restored source backup."""

import base64
import hashlib
import json
import re

from app.catalog.import_evidence import RETIRED_FIELDS
from app.catalog.schema import build_metadata
from scripts.catalog_manual_foundation import _verify_models
from scripts.catalog_sql import _editor_block


PREVIOUS_STEPS = '["initialized","foundation","taxonomy","aliases","documents","metadata_details","credits","locations","previews"]'
SOURCE_TABLES = ('document', 'metadata', 'classification')
FIELDS = {'evidence_id','md5','record_kind','record_key','source','payload',
          'source_table','source_key','source_hash'}


def _payload(table, alias):
    removed=','.join("'"+name+"'" for name in sorted(RETIRED_FIELDS[table]))
    unknown=f'(to_jsonb({alias})-ARRAY[{removed}]::text[])'
    if table=='classification':
        return unknown+"||jsonb_build_object('path_en',c.path_en,'path_en_key',c.path_en_key,'path_tt',c.path_tt)"
    if table=='metadata':
        return unknown+"""||coalesce((SELECT jsonb_build_object('original_managed_terms',jsonb_agg(value ORDER BY ordinal))
            FROM jsonb_array_elements(CASE jsonb_typeof(m.schema_org::jsonb->'about')
                WHEN 'array' THEN m.schema_org::jsonb->'about'
                WHEN 'object' THEN jsonb_build_array(m.schema_org::jsonb->'about') ELSE '[]'::jsonb END)
                WITH ORDINALITY AS term(value,ordinal)
            WHERE m.classification_id IS NOT NULL AND jsonb_typeof(value->'inDefinedTermSet')='object'
                AND lower(value->'inDefinedTermSet'->>'name') IN ('ddc','categorypath') HAVING count(*)>0),'{}'::jsonb)"""
    return unknown+"""||CASE WHEN d.full IS NULL THEN jsonb_build_object('full',NULL) ELSE '{}'::jsonb END
        ||CASE WHEN d.sharing_restricted IS NULL THEN jsonb_build_object('sharing_restricted',NULL) ELSE '{}'::jsonb END
        ||CASE WHEN d.language IS DISTINCT FROM nullif(array_to_string(p.languages,','),'')
            THEN jsonb_build_object('language',d.language) ELSE '{}'::jsonb END
        ||CASE WHEN d.sharing_restricted IS NOT FALSE AND d.ya_public_url IS NOT NULL
            THEN jsonb_build_object('ya_public_url',d.ya_public_url) ELSE '{}'::jsonb END
        ||CASE WHEN d.sharing_restricted IS NOT FALSE AND d.ya_public_key IS NOT NULL
            THEN jsonb_build_object('ya_public_key',d.ya_public_key) ELSE '{}'::jsonb END"""


def render_manual_evidence(fingerprint, schema='monocorpus', dataset_schema='public', *, offset, total, records,
                           payload_signature):
    build_metadata(schema)
    build_metadata(dataset_schema)
    if not isinstance(fingerprint,str) or not re.fullmatch(r'[0-9a-f]{64}',fingerprint):
        raise ValueError('invalid reviewed fingerprint')
    if not isinstance(payload_signature,str) or not re.fullmatch(r'[0-9a-f]{64}',payload_signature):
        raise ValueError('invalid reviewed payload signature')
    if type(offset) is not int or offset<0:
        raise ValueError('invalid evidence offset')
    if type(total) is not int or total<=0:
        raise ValueError('invalid evidence total')
    if not isinstance(records,list) or not 1<=len(records)<=5000 or offset+len(records)>total:
        raise ValueError('evidence requires 1 to 5000 reviewed records within total')
    seen=set()
    for position,row in enumerate(records,offset+1):
        if not isinstance(row,dict) or set(row)!=FIELDS:
            raise ValueError('invalid evidence record fields')
        table=row['source_table']
        if table not in SOURCE_TABLES or row['source']!='legacy.'+table:
            raise ValueError('invalid evidence source')
        if type(row['evidence_id']) is not int or row['evidence_id']!=position or row['record_kind']!='import':
            raise ValueError('evidence IDs must follow the reviewed offset')
        if not isinstance(row['record_key'],str) or not re.fullmatch(table+r':(0|[1-9][0-9]*)',row['record_key']):
            raise ValueError('invalid evidence record key')
        if row['record_key'] in seen:
            raise ValueError('duplicate evidence record key')
        seen.add(row['record_key'])
        if not isinstance(row['source_hash'],str) or not re.fullmatch(r'[0-9a-f]{64}',row['source_hash']):
            raise ValueError('invalid evidence source hash')
        key=row['source_key']
        if table=='classification':
            if row['md5'] is not None or not isinstance(key,str) or not re.fullmatch(r'[1-9][0-9]*',key):
                raise ValueError('invalid classification source key')
        elif not isinstance(key,str) or not re.fullmatch(r'[0-9a-f]{32}',key) or row['md5']!=key:
            raise ValueError('invalid document source key')
        if not isinstance(row['payload'],dict) or not row['payload']:
            raise ValueError('nonempty evidence payload required')
    controls=[[SOURCE_TABLES.index(row['source_table']),row['source_key'],int(row['record_key'].split(':')[1])]
              for row in records]
    raw=json.dumps(controls,separators=(',',':'),allow_nan=False).encode()
    if len(raw)>4*1024*1024:
        raise ValueError('evidence batch exceeds 4 MB, reduce its size')
    encoded=base64.b64encode(raw).decode('ascii')
    source_signature=hashlib.sha256(''.join(row['source_hash'] for row in records).encode()).hexdigest()
    source_checks=[]
    for table in SOURCE_TABLES:
        source_checks.append(f"""IF (SELECT count(*) FROM "{dataset_schema}"."{table}") IS DISTINCT FROM
            (migration.manifest->'source_tables'->'{table}'->>'rows')::bigint THEN
            RAISE EXCEPTION 'evidence source count changed for {table}'; END IF;""")
    columns='evidence_id,md5,record_kind,record_key,source,payload'
    model={'evidence':{'key':'evidence_id','ignored_columns':('created_at',),
        'query':f'SELECT {columns} FROM pg_temp.catalog_manual_evidence_input',
        'target_query':f'SELECT * FROM "{schema}".catalog_evidence WHERE evidence_id>{offset} AND evidence_id<={offset+len(records)}'}}
    verify=_verify_models(schema,model,phase='evidence')
    sources=','.join(f'"{dataset_schema}"."{name}"' for name in SOURCE_TABLES)
    source_sql='\n'.join(source_checks)
    payloads={name:_payload(name,alias) for name,alias in zip(SOURCE_TABLES,('d','m','c'),strict=True)}
    final=f"""UPDATE "{schema}".catalog_imports SET manifest=jsonb_set(manifest,'{{manual_completed_steps}}',
        manifest->'manual_completed_steps'||'["evidence"]'::jsonb) WHERE import_id=migration.import_id;""" if offset+len(records)==total else ''
    body=f"""DECLARE
    migration "{schema}".catalog_imports;
    loaded bigint;
BEGIN
    SET LOCAL lock_timeout='10s';
    SET LOCAL work_mem='4MB';
    SET LOCAL TimeZone='UTC';
    IF NOT pg_try_advisory_xact_lock(hashtext('catalog-import:{schema}')) THEN
        RAISE EXCEPTION 'another catalog migration is running'; END IF;
    SELECT * INTO migration FROM "{schema}".catalog_imports WHERE source_fingerprint='{fingerprint}' FOR UPDATE;
    IF migration.import_id IS NULL OR migration.state<>'loading'
        OR migration.manifest->'manual_editor' IS DISTINCT FROM 'true'::jsonb
        OR migration.manifest->>'evidence_mode' IS DISTINCT FROM 'essential'
        OR migration.manifest->>'dataset_schema' IS DISTINCT FROM '{dataset_schema}' THEN
        RAISE EXCEPTION 'reviewed manual essential loading import required'; END IF;
    IF NOT coalesce(migration.manifest->'manual_completed_steps' @> '["evidence"]'::jsonb,false)
        AND migration.manifest->'manual_completed_steps' IS DISTINCT FROM '{PREVIOUS_STEPS}'::jsonb THEN
        RAISE EXCEPTION 'complete previews required before evidence'; END IF;
    LOCK TABLE "{schema}".catalog_evidence,"{schema}".catalog_documents,
        "{schema}".catalog_publications,{sources} IN SHARE ROW EXCLUSIVE MODE;
    loaded:=coalesce((migration.manifest->'manual_evidence_progress'->>'loaded_evidence')::bigint,0);
    IF loaded<0 OR loaded>{total} OR (loaded<>0 AND
        (migration.manifest->'manual_evidence_progress'->>'total_evidence')::bigint IS DISTINCT FROM {total}) THEN
        RAISE EXCEPTION 'evidence progress changed'; END IF;
    IF (SELECT count(*) FROM "{schema}".catalog_evidence)<>loaded OR
        coalesce((SELECT max(evidence_id) FROM "{schema}".catalog_evidence),0)<>loaded OR
        EXISTS(SELECT 1 FROM "{schema}".catalog_evidence WHERE evidence_id<1) THEN
        RAISE EXCEPTION 'evidence contains untracked rows'; END IF;
    IF loaded<{offset+len(records)} AND loaded<>{offset} THEN
        RAISE EXCEPTION 'evidence batch is out of order'; END IF;
    CREATE TEMP TABLE catalog_manual_evidence_input ON COMMIT DROP AS
        WITH controls AS (SELECT {offset}+ordinal AS evidence_id,value->>1 AS source_key,
            value->>2 AS original_position,CASE (value->>0)::integer
                WHEN 0 THEN 'document' WHEN 1 THEN 'metadata' WHEN 2 THEN 'classification' END AS source_table
            FROM jsonb_array_elements(convert_from(decode('{encoded}','base64'),'UTF8')::jsonb)
                WITH ORDINALITY AS entry(value,ordinal))
        SELECT e.evidence_id,CASE WHEN e.source_table='classification' THEN NULL ELSE e.source_key END AS md5,
            'import'::text AS record_kind,e.source_table||':'||e.original_position AS record_key,
            'legacy.'||e.source_table AS source,
            CASE e.source_table WHEN 'document' THEN {payloads['document']}
                WHEN 'metadata' THEN {payloads['metadata']} WHEN 'classification' THEN {payloads['classification']} END AS payload,
            encode(sha256(convert_to(CASE e.source_table WHEN 'document' THEN to_jsonb(d)::text
                WHEN 'metadata' THEN to_jsonb(m)::text WHEN 'classification' THEN to_jsonb(c)::text END,'UTF8')),'hex') AS source_hash
        FROM controls e
        LEFT JOIN "{dataset_schema}".document d ON e.source_table='document' AND d.md5=e.source_key
        LEFT JOIN "{dataset_schema}".metadata m ON e.source_table='metadata' AND m.md5=e.source_key
        LEFT JOIN "{dataset_schema}".classification c ON c.id=CASE WHEN e.source_table='classification' THEN e.source_key::integer END
        LEFT JOIN "{schema}".catalog_documents target_d ON d.md5=target_d.md5
        LEFT JOIN "{schema}".catalog_publications p ON target_d.publication_id=p.publication_id;
    {source_sql}
    IF (SELECT encode(sha256(convert_to(string_agg(source_hash,'' ORDER BY evidence_id),'UTF8')),'hex')
        FROM pg_temp.catalog_manual_evidence_input) IS DISTINCT FROM '{source_signature}' THEN
        RAISE EXCEPTION 'evidence source changed'; END IF;
    IF (SELECT encode(sha256(convert_to(string_agg(encode(sha256(convert_to(payload::text,'UTF8')),'hex'),''
        ORDER BY evidence_id),'UTF8')),'hex') FROM pg_temp.catalog_manual_evidence_input) IS DISTINCT FROM '{payload_signature}' THEN
        RAISE EXCEPTION 'evidence payload mapping differs from reviewed backup'; END IF;
    IF loaded>={offset+len(records)} THEN
        {verify}
        RETURN;
    END IF;
    INSERT INTO "{schema}".catalog_evidence ({columns}) SELECT {columns} FROM pg_temp.catalog_manual_evidence_input;
    {verify}
    PERFORM setval(pg_get_serial_sequence('"{schema}".catalog_evidence','evidence_id'),{offset+len(records)},true);
    UPDATE "{schema}".catalog_imports SET manifest=jsonb_set(manifest,'{{manual_evidence_progress}}',
        jsonb_build_object('loaded_evidence',{offset+len(records)},'total_evidence',{total})) WHERE import_id=migration.import_id;
    {final}
END;"""
    return _editor_block(body)
