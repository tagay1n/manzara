DO $manzara_personality$
DECLARE
    request jsonb := %(payload)s::jsonb;
    observed jsonb := request->'source';
    decision jsonb := request->'decision';
    components jsonb := request->'components';
    raw text := request->>'raw_name';
    stamp text := request->>'now';
    state_name text := CASE WHEN decision->>'outcome'='normalized' THEN 'succeeded' ELSE decision->>'outcome' END;
    reason_text text := decision->>'reason';
    identity text := request->>'identity_key';
    lock_key text;
    publications bigint[];
    matches bigint[];
    entity_key bigint;
    observed_name bigint;
    target_count bigint;
    role_added bigint;
    alias_after jsonb;
    entity_before jsonb;
    entity_after jsonb;
    review_before jsonb;
    review_after jsonb;
    protected_reviews jsonb;
    protected_review jsonb;
    checkpoint_before jsonb;
    checkpoint_after jsonb;
    fresh jsonb;
    write_review boolean;
BEGIN
    PERFORM set_config('lock_timeout', %(lock_timeout)s, true);
    PERFORM set_config('manzara.catalog_actor', 'task', true);
    FOR lock_key IN SELECT key FROM unnest(ARRAY['name:'||raw, 'identity:'||identity]) AS keys(key)
        WHERE key IS NOT NULL ORDER BY key LOOP
        PERFORM pg_advisory_xact_lock(hashtext('personality-normalization:'||current_schema()),hashtext(lock_key));
    END LOOP;

    SELECT to_jsonb(cp) INTO checkpoint_before FROM personality_normalization_checkpoints cp
        WHERE cp.raw_name=raw FOR UPDATE;
    IF checkpoint_before IS DISTINCT FROM NULLIF(observed->'durable_checkpoint','null'::jsonb) THEN
        RAISE EXCEPTION USING ERRCODE='MZ001', MESSAGE='Personality checkpoint changed; resume with a fresh batch';
    END IF;

    -- Match metadata's document-before-publication lock order. Lock source child rows
    -- too, so a concurrent direct catalog edit cannot change the guarded evidence.
    SELECT array_agg(DISTINCT p.publication_id) INTO publications
        FROM catalog_names n JOIN catalog_contributions c USING(name_id)
        JOIN catalog_publications p USING(publication_id)
        WHERE n.kind='person' AND {{compact_name}}=raw
          AND c.role IN ('author','editor','translator','illustrator','contributor')
          AND p.inclusion='included' AND p.has_metadata AND p.metadata_present;
    PERFORM d.md5 FROM catalog_documents d WHERE d.publication_id=ANY(publications) ORDER BY d.md5 FOR UPDATE;
    PERFORM p.publication_id FROM catalog_publications p
        WHERE p.publication_id=ANY(publications) ORDER BY p.publication_id FOR UPDATE;
    PERFORM c.contribution_id FROM catalog_contributions c
        WHERE c.publication_id=ANY(publications) ORDER BY c.contribution_id FOR SHARE;
    PERFORM l.publication_id FROM catalog_publication_languages l
        WHERE l.publication_id=ANY(publications) ORDER BY l.publication_id,l.position FOR SHARE;
    PERFORM n.name_id FROM catalog_names n WHERE n.kind='person' AND {{compact_name}}=raw ORDER BY n.name_id FOR SHARE;
    SELECT to_jsonb(candidate) INTO fresh FROM ({{source_query}}) candidate;
    IF fresh IS NULL OR fresh IS DISTINCT FROM (observed-'durable_checkpoint') THEN
        RAISE EXCEPTION USING ERRCODE='MZ001', MESSAGE='Personality sources changed; resume with a fresh batch';
    END IF;

    IF state_name='succeeded' THEN
        -- Serialize even incompatible rows sharing the same identity key. Titles
        -- do not participate in compatibility, while sex and name components do.
        PERFORM e.entity_id FROM catalog_entities e WHERE e.kind='person' AND e.status='active'
            AND e.identity_key=identity ORDER BY e.entity_id FOR UPDATE;
        SELECT array_agg(e.entity_id ORDER BY e.entity_id) INTO matches FROM catalog_entities e
            WHERE e.kind='person' AND e.status='active' AND e.identity_key=identity
              AND NOT EXISTS (SELECT 1 FROM jsonb_each_text(components) field
                  WHERE field.key<>'title' AND NULLIF(field.value,'') IS NOT NULL
                    AND NULLIF(to_jsonb(e)->>field.key,'') IS NOT NULL
                    AND to_jsonb(e)->>field.key<>field.value);
        IF (checkpoint_before->>'canonical_id')::bigint=ANY(matches) THEN
            entity_key := (checkpoint_before->>'canonical_id')::bigint;
        ELSIF cardinality(matches)>1 THEN
            state_name := 'failed';
            entity_key := (checkpoint_before->>'canonical_id')::bigint;
            reason_text := 'Multiple compatible identities; review this source name before retrying.';
        ELSE
            entity_key := matches[1];
        END IF;
    END IF;

    IF state_name='succeeded' THEN
        IF entity_key IS NULL THEN
            INSERT INTO catalog_entities(kind,display_name,approval,identity_key,status,created_at,
                surname_full,surname_initials,name_full,name_initials,father_name_full,father_name_initials,title,sex)
            VALUES('person',request->>'display_name','unconfirmed',identity,'active',stamp,
                components->>'surname_full',components->>'surname_initials',components->>'name_full',components->>'name_initials',
                components->>'father_name_full',components->>'father_name_initials',components->>'title',components->>'sex')
            RETURNING entity_id,to_jsonb(catalog_entities) INTO entity_key,entity_after;
            INSERT INTO catalog_revisions(record_kind,record_key,actor,before,after)
                VALUES('entity',entity_key::text,'task',NULL,entity_after);
        END IF;
        INSERT INTO catalog_entity_roles(entity_id,role) VALUES(entity_key,'personality')
            ON CONFLICT DO NOTHING RETURNING entity_id INTO role_added;
        INSERT INTO catalog_names(kind,raw_name) VALUES('person',raw)
            ON CONFLICT(kind,raw_name) DO UPDATE SET raw_name=excluded.raw_name RETURNING name_id INTO observed_name;
        PERFORM r.alias_id FROM catalog_alias_reviews r WHERE r.name_id=observed_name ORDER BY r.alias_id FOR UPDATE;
        SELECT COALESCE(jsonb_agg(to_jsonb(r)),'[]'::jsonb) INTO protected_reviews FROM catalog_alias_reviews r
            WHERE r.name_id=observed_name AND (r.entity_type<>'personality'
                OR r.source IS DISTINCT FROM 'gemini_personality_normalizer');
        SELECT to_jsonb(r) INTO review_before FROM catalog_alias_reviews r
            WHERE r.entity_type='personality' AND r.name_id=observed_name FOR UPDATE;
        write_review := review_before IS NULL OR review_before->>'source'='gemini_personality_normalizer';
        INSERT INTO catalog_aliases(name_id,entity_id,approval) VALUES(observed_name,entity_key,'unconfirmed')
            ON CONFLICT DO NOTHING RETURNING to_jsonb(catalog_aliases) INTO alias_after;
        IF alias_after IS NOT NULL THEN
            INSERT INTO catalog_revisions(record_kind,record_key,actor,before,after)
                VALUES('alias',alias_after->>'alias_id','task',NULL,alias_after);
        END IF;
        IF alias_after IS NOT NULL OR role_added IS NOT NULL THEN
            SELECT to_jsonb(e) INTO entity_before FROM catalog_entities e WHERE e.entity_id=entity_key;
            UPDATE catalog_entities SET revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE entity_id=entity_key
                RETURNING to_jsonb(catalog_entities) INTO entity_after;
            INSERT INTO catalog_revisions(record_kind,record_key,actor,before,after)
                VALUES('entity',entity_key::text,'task',entity_before,entity_after);
        END IF;
        SELECT count(*) INTO target_count FROM catalog_aliases a JOIN catalog_entities e USING(entity_id)
            WHERE a.name_id=observed_name AND e.status='active';
        IF write_review THEN
            -- Read the trigger-created placeholder as the actual audited before row.
            SELECT to_jsonb(r) INTO review_before FROM catalog_alias_reviews r
                WHERE r.entity_type='personality' AND r.name_id=observed_name FOR UPDATE;
            INSERT INTO catalog_alias_reviews(entity_type,name_id,entity_id,normalized_name,script_label,decision_status,
                docs_count,mentions_count,marker_count,confidence,source,reason,successful_model,prompt_version,schema_version,
                source_roles,created_at,updated_at,surname_full,surname_initials,name_full,name_initials,
                father_name_full,father_name_initials,title,sex)
            VALUES('personality',observed_name,CASE WHEN target_count=1 THEN entity_key END,identity,'other',
                CASE WHEN target_count=1 THEN 'linked' ELSE 'pending' END,(observed->>'document_count')::bigint,
                (observed->>'mention_count')::bigint,0,1.0,'gemini_personality_normalizer','structured_success',
                request->>'model',request->>'prompt_version',request->>'schema_version',observed->'source_roles',stamp,stamp,
                components->>'surname_full',components->>'surname_initials',components->>'name_full',components->>'name_initials',
                components->>'father_name_full',components->>'father_name_initials',components->>'title',components->>'sex')
            ON CONFLICT(entity_type,name_id) DO UPDATE SET entity_id=excluded.entity_id,normalized_name=excluded.normalized_name,
                script_label=excluded.script_label,decision_status=excluded.decision_status,docs_count=excluded.docs_count,
                mentions_count=excluded.mentions_count,marker_count=excluded.marker_count,confidence=excluded.confidence,
                source=excluded.source,reason=excluded.reason,successful_model=excluded.successful_model,
                prompt_version=excluded.prompt_version,schema_version=excluded.schema_version,source_roles=excluded.source_roles,
                updated_at=excluded.updated_at,surname_full=excluded.surname_full,surname_initials=excluded.surname_initials,
                name_full=excluded.name_full,name_initials=excluded.name_initials,father_name_full=excluded.father_name_full,
                father_name_initials=excluded.father_name_initials,title=excluded.title,sex=excluded.sex
            RETURNING to_jsonb(catalog_alias_reviews) INTO review_after;
            INSERT INTO catalog_revisions(record_kind,record_key,actor,before,after)
                VALUES('alias_review',review_after->>'alias_id','task',review_before,review_after);
        END IF;
        -- The alias trigger refreshes every review for this name, including other
        -- entity types. Preserve every decision not owned by this normalizer.
        FOR protected_review IN SELECT value FROM jsonb_array_elements(protected_reviews) LOOP
            UPDATE catalog_alias_reviews SET entity_id=(protected_review->>'entity_id')::bigint,
                decision_status=protected_review->>'decision_status',updated_at=protected_review->>'updated_at'
                WHERE alias_id=(protected_review->>'alias_id')::bigint;
        END LOOP;
    END IF;

    INSERT INTO personality_normalization_checkpoints(raw_name,source_fingerprint,document_count,mention_count,source_roles,
        prompt_version,schema_version,state,decision_reason,successful_model,decision_evidence,canonical_id,updated_at,completed_at)
    VALUES(raw,observed->>'source_fingerprint',(observed->>'document_count')::bigint,(observed->>'mention_count')::bigint,
        observed->'source_roles',request->>'prompt_version',request->>'schema_version',state_name,reason_text,request->>'model',
        decision||jsonb_build_object('document_languages',observed->'document_languages'),entity_key,stamp,
        CASE WHEN state_name<>'failed' THEN stamp END)
    ON CONFLICT(raw_name) DO UPDATE SET source_fingerprint=excluded.source_fingerprint,document_count=excluded.document_count,
        mention_count=excluded.mention_count,source_roles=excluded.source_roles,prompt_version=excluded.prompt_version,
        schema_version=excluded.schema_version,state=excluded.state,decision_reason=excluded.decision_reason,
        successful_model=excluded.successful_model,decision_evidence=excluded.decision_evidence,canonical_id=excluded.canonical_id,
        updated_at=excluded.updated_at,completed_at=excluded.completed_at
    RETURNING to_jsonb(personality_normalization_checkpoints) INTO checkpoint_after;
    INSERT INTO catalog_revisions(record_kind,record_key,actor,before,after)
        VALUES('personality_checkpoint',raw,'task',checkpoint_before,checkpoint_after);
    PERFORM set_config('manzara.personality_result',jsonb_build_object('state',state_name,'canonical_id',entity_key,
        'reason',reason_text,'durable_checkpoint',checkpoint_after)::text,true);
END;
$manzara_personality$;
SELECT current_setting('manzara.personality_result')::jsonb;
