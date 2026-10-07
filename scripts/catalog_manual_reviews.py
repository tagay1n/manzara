"""Offline, source-checked transfer of outstanding identity review decisions."""

import re

from app.catalog.schema import build_metadata
from scripts.catalog_manual_foundation import _verify_models
from scripts.catalog_sql import _editor_block


SOURCE_TABLES=('normalization_suggestions','publisher_merge_proposals','publisher_separations')
PREVIOUS_STEPS='["initialized","foundation","taxonomy","aliases","documents","metadata_details","credits","locations","previews","evidence"]'


def _publisher_kinds(schema):
    return f"""SELECT DISTINCT n.kind,n.raw_name,0 AS phase FROM "{schema}".catalog_names n
        JOIN "{schema}".catalog_contributions c USING(name_id) WHERE c.role='publisher'
        UNION SELECT DISTINCT n.kind,n.raw_name,1 AS phase FROM "{schema}".catalog_names n
        JOIN "{schema}".catalog_aliases a USING(name_id)
        JOIN "{schema}".catalog_entity_roles r ON r.entity_id=a.entity_id WHERE r.role='publisher'"""


def _signature(query):
    return f"""SELECT encode(sha256(convert_to(coalesce(string_agg(h,'' ORDER BY h),''),'UTF8')),'hex')
        FROM (SELECT encode(sha256(convert_to(payload::text,'UTF8')),'hex') AS h FROM ({query}) rows) hashes"""


def review_signature_queries(schema='monocorpus', *, base_name_max_id):
    build_metadata(schema)
    if type(base_name_max_id) is not int or base_name_max_id<0:
        raise ValueError('invalid base name maximum')
    queries={name:f'SELECT to_jsonb(s) AS payload FROM "{schema}"."{name}" s' for name in SOURCE_TABLES}
    queries.update({
        'names':f'SELECT to_jsonb(n) AS payload FROM "{schema}".catalog_names n WHERE name_id<={base_name_max_id}',
        'entities':f'SELECT to_jsonb(e)-\'updated_at\' AS payload FROM "{schema}".catalog_entities e',
        'publisher_kinds':f'SELECT to_jsonb(k) AS payload FROM ({_publisher_kinds(schema)}) k',
    })
    return {key:_signature(query) for key,query in queries.items()}


def _preparations(schema, base):
    s=f'"{schema}"'
    return f"""CREATE TEMP TABLE catalog_manual_review_walk ON COMMIT DROP AS
    WITH RECURSIVE walk AS (
        SELECT entity_id AS original_id,entity_id,merged_into_id,ARRAY[entity_id] AS visited,false AS cycle
        FROM {s}.catalog_entities
        UNION ALL SELECT w.original_id,e.entity_id,e.merged_into_id,w.visited||e.entity_id,e.entity_id=ANY(w.visited)
        FROM walk w JOIN {s}.catalog_entities e ON e.entity_id=w.merged_into_id WHERE NOT w.cycle
    ) SELECT * FROM walk;
    IF EXISTS(SELECT 1 FROM pg_temp.catalog_manual_review_walk WHERE cycle) THEN
        RAISE EXCEPTION 'identity merge cycle before review loading'; END IF;
    CREATE TEMP TABLE catalog_manual_review_roots ON COMMIT DROP AS
        SELECT original_id,entity_id FROM pg_temp.catalog_manual_review_walk WHERE merged_into_id IS NULL;
    IF (SELECT count(*) FROM pg_temp.catalog_manual_review_roots)<>(SELECT count(*) FROM {s}.catalog_entities) THEN
        RAISE EXCEPTION 'missing identity merge target before review loading'; END IF;
    CREATE UNIQUE INDEX ON catalog_manual_review_roots(original_id);
    CREATE TEMP TABLE catalog_manual_review_sources ON COMMIT DROP AS
    WITH candidates AS (
        SELECT 0 AS phase,n.suggestion_id AS source_id,'normalization_suggestions'::text AS source_table,
            n.normalized_name AS display_name,'pending'::text AS status,
            jsonb_build_object('source','legacy.normalization_suggestions','legacy_id',n.suggestion_id,
                'legacy_status',n.status,'original',to_jsonb(n)) AS evidence,
            jsonb_build_array('raw:'||n.raw_name)||CASE WHEN n.target_canonical_id IS NULL THEN '[]'::jsonb
                ELSE jsonb_build_array('canonical:'||n.target_canonical_id) END AS raw_keys,
            CASE n.entity_type WHEN 'personality' THEN 'person' ELSE 'organization' END AS forced_kind
        FROM {s}.normalization_suggestions n WHERE n.status='open'
        UNION ALL
        SELECT 1,p.proposal_id,'publisher_merge_proposals',
            coalesce(nullif(reviewed.value->>'display_name',''),p.proposal->>'proposed_name'),
            CASE p.status WHEN 'skipped' THEN 'deferred' ELSE 'pending' END,
            jsonb_build_object('source','legacy.publisher_merge_proposals','legacy_id',p.proposal_id,
                'legacy_status',p.status,'original',to_jsonb(p)),reviewed.value->'member_ids',NULL::text
        FROM {s}.publisher_merge_proposals p CROSS JOIN LATERAL (SELECT CASE WHEN p.review_edit IS NULL
            OR p.review_edit IN ('null'::jsonb,'{{}}'::jsonb) THEN p.proposal ELSE p.review_edit END AS value) reviewed
        WHERE p.status IN ('pending','staged','skipped')
    ) SELECT row_number() OVER(ORDER BY phase,source_id) AS proposal_id,* FROM candidates;
    IF EXISTS(SELECT 1 FROM pg_temp.catalog_manual_review_sources WHERE jsonb_typeof(raw_keys) IS DISTINCT FROM 'array') THEN
        RAISE EXCEPTION 'invalid legacy review member list'; END IF;
    IF EXISTS(SELECT 1 FROM pg_temp.catalog_manual_review_sources s
        CROSS JOIN LATERAL jsonb_array_elements(s.raw_keys) k WHERE jsonb_typeof(k.value)<>'string') THEN
        RAISE EXCEPTION 'invalid legacy review member type'; END IF;
    CREATE TEMP TABLE catalog_manual_review_separation_sources ON COMMIT DROP AS
        SELECT row_number() OVER(ORDER BY left_key COLLATE "C",right_key COLLATE "C") AS group_id,*
        FROM {s}.publisher_separations;
    CREATE TEMP TABLE catalog_manual_review_requests ON COMMIT DROP AS
        SELECT 'proposal'::text AS group_type,s.proposal_id AS group_id,k.ordinal AS side,
            k.value AS member_key,s.forced_kind FROM pg_temp.catalog_manual_review_sources s
        CROSS JOIN LATERAL jsonb_array_elements_text(s.raw_keys) WITH ORDINALITY k(value,ordinal)
        UNION ALL SELECT 'separation',group_id,0,left_key,NULL FROM pg_temp.catalog_manual_review_separation_sources
        UNION ALL SELECT 'separation',group_id,1,right_key,NULL FROM pg_temp.catalog_manual_review_separation_sources;
    IF EXISTS(SELECT 1 FROM pg_temp.catalog_manual_review_requests WHERE NOT
        (member_key ~ '^canonical:[0-9]+$' OR (left(member_key,4)='raw:' AND length(member_key)>4))) THEN
        RAISE EXCEPTION 'unsupported legacy review identity'; END IF;
    CREATE TEMP TABLE catalog_manual_review_kind_inventory ON COMMIT DROP AS {_publisher_kinds(schema)};
    CREATE TEMP TABLE catalog_manual_review_kinds ON COMMIT DROP AS
        WITH first_phase AS (SELECT raw_name,min(phase) AS phase
            FROM pg_temp.catalog_manual_review_kind_inventory GROUP BY raw_name)
        SELECT k.raw_name,min(k.kind) AS kind,count(DISTINCT k.kind) AS kinds
        FROM pg_temp.catalog_manual_review_kind_inventory k JOIN first_phase USING(raw_name,phase)
        GROUP BY k.raw_name;
    IF EXISTS(SELECT 1 FROM pg_temp.catalog_manual_review_requests r
        JOIN pg_temp.catalog_manual_review_kinds k ON k.raw_name=substring(r.member_key FROM 5)
        WHERE left(r.member_key,4)='raw:' AND r.forced_kind IS NULL AND k.kinds>1) THEN
        RAISE EXCEPTION 'ambiguous publisher suggestion name type'; END IF;
    CREATE TEMP TABLE catalog_manual_review_resolved ON COMMIT DROP AS
        SELECT r.group_type,r.group_id,r.side,root.entity_id,
            CASE WHEN left(r.member_key,4)='raw:' THEN coalesce(r.forced_kind,k.kind,'organization') END AS kind,
            CASE WHEN left(r.member_key,4)='raw:' THEN substring(r.member_key FROM 5) END AS raw_name
        FROM pg_temp.catalog_manual_review_requests r LEFT JOIN pg_temp.catalog_manual_review_roots root
            ON root.original_id=CASE WHEN r.member_key ~ '^canonical:[0-9]+$' THEN substring(r.member_key FROM 11)::bigint END
        LEFT JOIN pg_temp.catalog_manual_review_kinds k ON left(r.member_key,4)='raw:' AND k.raw_name=substring(r.member_key FROM 5);
    IF EXISTS(SELECT 1 FROM pg_temp.catalog_manual_review_resolved WHERE entity_id IS NULL AND raw_name IS NULL) THEN
        RAISE EXCEPTION 'legacy review identity not found'; END IF;
    CREATE TEMP TABLE catalog_manual_expected_review_names ON COMMIT DROP AS
        WITH missing AS (SELECT DISTINCT r.kind,r.raw_name FROM pg_temp.catalog_manual_review_resolved r
            LEFT JOIN {s}.catalog_names n ON n.name_id<={base} AND n.kind=r.kind AND n.raw_name=r.raw_name
            WHERE r.entity_id IS NULL AND n.name_id IS NULL)
        SELECT {base}+row_number() OVER(ORDER BY kind COLLATE "C",raw_name COLLATE "C") AS name_id,kind,raw_name FROM missing;
    CREATE UNIQUE INDEX ON catalog_manual_expected_review_names(kind,raw_name);
    CREATE TEMP TABLE catalog_manual_review_members_resolved ON COMMIT DROP AS
        SELECT r.*,coalesce(n.name_id,extra.name_id) AS name_id
        FROM pg_temp.catalog_manual_review_resolved r LEFT JOIN {s}.catalog_names n
            ON n.name_id<={base} AND n.kind=r.kind AND n.raw_name=r.raw_name
        LEFT JOIN pg_temp.catalog_manual_expected_review_names extra ON extra.kind=r.kind AND extra.raw_name=r.raw_name;
    CREATE TEMP TABLE catalog_manual_expected_review_proposals ON COMMIT DROP AS
        SELECT proposal_id,'identity'::text AS kind,status,display_name,evidence,'{{}}'::jsonb AS field_changes,
            NULL::bigint AS publication_id,1::bigint AS revision FROM pg_temp.catalog_manual_review_sources;
    CREATE TEMP TABLE catalog_manual_expected_review_members ON COMMIT DROP AS
        WITH members AS (SELECT DISTINCT r.group_id AS proposal_id,r.entity_id,r.name_id,
            CASE WHEN r.entity_id IS NOT NULL THEN e.revision END AS reviewed_revision,
            CASE WHEN r.entity_id IS NOT NULL THEN jsonb_build_object('kind',e.kind,'display_name',e.display_name)
                ELSE jsonb_build_object('kind',r.kind,'raw_name',r.raw_name) END AS snapshot
            FROM pg_temp.catalog_manual_review_members_resolved r LEFT JOIN {s}.catalog_entities e USING(entity_id)
            WHERE group_type='proposal')
        SELECT row_number() OVER(ORDER BY proposal_id,entity_id NULLS LAST,name_id NULLS LAST) AS member_id,* FROM members;
    CREATE TEMP TABLE catalog_manual_expected_review_separations ON COMMIT DROP AS
        WITH keyed AS (SELECT group_id,side,CASE WHEN entity_id IS NOT NULL THEN 'entity:'||entity_id ELSE 'name:'||name_id END AS key
            FROM pg_temp.catalog_manual_review_members_resolved WHERE group_type='separation')
        SELECT DISTINCT least(l.key COLLATE "C",r.key COLLATE "C") AS left_key,
            greatest(l.key COLLATE "C",r.key COLLATE "C") AS right_key,migration.manifest->>'actor' AS actor
        FROM keyed l JOIN keyed r USING(group_id) WHERE l.side=0 AND r.side=1 AND l.key<>r.key;
    """


def render_manual_reviews(fingerprint, schema='monocorpus', dataset_schema='public', *, base_name_max_id, signatures):
    metadata=build_metadata(schema)
    build_metadata(dataset_schema)
    queries=review_signature_queries(schema,base_name_max_id=base_name_max_id)
    if not isinstance(fingerprint,str) or not re.fullmatch(r'[0-9a-f]{64}',fingerprint):
        raise ValueError('invalid reviewed fingerprint')
    if not isinstance(signatures,dict) or set(signatures)!=set(queries) or any(
        not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{64}',value) for value in signatures.values()):
        raise ValueError('invalid review source/context signatures')
    source_checks='\n'.join(f"""IF (SELECT count(*) FROM "{schema}"."{name}") IS DISTINCT FROM
        (migration.manifest->'source_tables'->'{name}'->>'rows')::bigint THEN
        RAISE EXCEPTION 'review source count changed for {name}'; END IF;""" for name in SOURCE_TABLES)
    signature_checks='\n'.join(f"""IF ({query}) IS DISTINCT FROM '{signatures[name]}' THEN
        RAISE EXCEPTION 'review source changed for {name}'; END IF;""" for name,query in queries.items())
    specifications={
        'names':('name_id','name_id,kind,raw_name','names'),
        'proposals':('proposal_id','proposal_id,kind,status,display_name,evidence,field_changes,publication_id,revision','proposals'),
        'proposal_members':('member_id','member_id,proposal_id,entity_id,name_id,reviewed_revision,snapshot','members'),
        'separations':('left_key,right_key','left_key,right_key,actor','separations'),
    }
    models={name:{'key':key,'ignored_columns':('updated_at',) if name=='proposals' else (),
        'query':f'SELECT {columns} FROM pg_temp.catalog_manual_expected_review_{temp}'}
        for name,(key,columns,temp) in specifications.items()}
    models['names']['target_query']=f'SELECT * FROM "{schema}".catalog_names WHERE name_id>{base_name_max_id}'
    verification=_verify_models(schema,models,phase='review')
    inserts='\n'.join(f'INSERT INTO "{schema}".catalog_{name} ({columns}) SELECT {columns} FROM pg_temp.catalog_manual_expected_review_{temp};'
        for name,(_,columns,temp) in specifications.items())
    sequences='\n'.join(f"""PERFORM setval(pg_get_serial_sequence('"{schema}".catalog_{name}','{key}'),
        coalesce((SELECT max({key}) FROM "{schema}".catalog_{name}),1),EXISTS(SELECT 1 FROM "{schema}".catalog_{name}));"""
        for name,(key,_,_) in specifications.items() if name!='separations')
    progress=f"""jsonb_build_object('base_name_max_id',{base_name_max_id},
        'base_names',(migration.manifest->'manual_credit_progress'->>'loaded_names')::bigint,
        'added_names',(SELECT count(*) FROM pg_temp.catalog_manual_expected_review_names),
        'loaded_proposals',(SELECT count(*) FROM pg_temp.catalog_manual_expected_review_proposals),
        'loaded_members',(SELECT count(*) FROM pg_temp.catalog_manual_expected_review_members),
        'loaded_separations',(SELECT count(*) FROM pg_temp.catalog_manual_expected_review_separations))"""
    locks=','.join(f'"{schema}"."{table.name}"' for table in sorted(metadata.tables.values(),key=lambda table:table.name))
    body=f"""DECLARE
    migration "{schema}".catalog_imports;
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
        OR migration.manifest->>'dataset_schema' IS DISTINCT FROM '{dataset_schema}'
        OR jsonb_typeof(migration.manifest->'actor') IS DISTINCT FROM 'string'
        OR btrim(migration.manifest->>'actor')='' THEN
        RAISE EXCEPTION 'reviewed manual essential loading import and actor required'; END IF;
    LOCK TABLE {locks} IN SHARE ROW EXCLUSIVE MODE;
    LOCK TABLE {','.join(f'"{schema}"."{name}"' for name in SOURCE_TABLES)} IN SHARE ROW EXCLUSIVE MODE;
    IF NOT coalesce(migration.manifest->'manual_completed_steps' @> '["reviews"]'::jsonb,false)
        AND migration.manifest->'manual_completed_steps' IS DISTINCT FROM '{PREVIOUS_STEPS}'::jsonb THEN
        RAISE EXCEPTION 'complete evidence required before reviews'; END IF;
    IF (SELECT count(*) FROM "{schema}".catalog_evidence) IS DISTINCT FROM
        (migration.manifest->'manual_evidence_progress'->>'loaded_evidence')::bigint OR
        migration.manifest->'manual_evidence_progress'->'loaded_evidence' IS DISTINCT FROM
        migration.manifest->'manual_evidence_progress'->'total_evidence' OR
        (SELECT count(*) FROM "{schema}".catalog_names WHERE name_id<={base_name_max_id}) IS DISTINCT FROM
        (migration.manifest->'manual_credit_progress'->>'loaded_names')::bigint THEN
        RAISE EXCEPTION 'preceding review context counts changed'; END IF;
    {source_checks}
    {signature_checks}
    IF EXISTS(SELECT 1 FROM "{schema}".catalog_protections) OR EXISTS(SELECT 1 FROM "{schema}".catalog_revisions) THEN
        RAISE EXCEPTION 'untracked protected edits during review import'; END IF;
    {_preparations(schema,base_name_max_id)}
    IF migration.manifest->'manual_completed_steps' @> '["reviews"]'::jsonb THEN
        {verification}
        IF migration.manifest->'manual_reviews_progress' IS DISTINCT FROM {progress} THEN
            RAISE EXCEPTION 'review progress changed'; END IF;
        RETURN;
    END IF;
    IF EXISTS(SELECT 1 FROM "{schema}".catalog_proposals) OR EXISTS(SELECT 1 FROM "{schema}".catalog_proposal_members)
        OR EXISTS(SELECT 1 FROM "{schema}".catalog_separations) OR
        coalesce((SELECT max(name_id) FROM "{schema}".catalog_names),0)<>{base_name_max_id} THEN
        RAISE EXCEPTION 'review staging contains untracked rows'; END IF;
    {inserts}
    {verification}
    {sequences}
    UPDATE "{schema}".catalog_imports SET manifest=jsonb_set(jsonb_set(manifest,
        '{{manual_reviews_progress}}',{progress}),'{{manual_completed_steps}}',
        manifest->'manual_completed_steps'||'["reviews"]'::jsonb) WHERE import_id=migration.import_id;
END;"""
    return _editor_block(body)
