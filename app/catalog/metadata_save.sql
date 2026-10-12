-- One simple-query message: PostgreSQL commits its implicit transaction only
-- after the result SELECT succeeds. Parameters stay outside the DO body.
SET LOCAL lock_timeout = %(lock_timeout)s;
SELECT set_config('manzara.metadata_save_request', %(payload)s::text, true);
DO $manzara_metadata_save$
DECLARE
    request jsonb := current_setting('manzara.metadata_save_request')::jsonb;
    publication_key bigint := (request->'snapshot'->'publication'->>'publication_id')::bigint;
    document_key text := request->>'md5';
    actor_name text := request->>'actor';
    plan jsonb := request->'plan';
    desired jsonb := request->'expected';
    evaluation jsonb := NULLIF(request->'evaluation', 'null'::jsonb);
    expected_path jsonb := request->'expected_classification_path';
    decision_changes jsonb;
    expected_decision jsonb;
    decision_retained boolean := false;
    classification_key bigint;
    taxonomy_parent bigint;
    taxonomy_node bigint;
    taxonomy_label text;
    validation_key text;
    old_publication jsonb;
    new_publication jsonb;
    old_document jsonb;
    fresh jsonb;
    metadata_after jsonb;
    actual jsonb;
    reference jsonb;
    proposal_key bigint;
    proposals jsonb := '[]'::jsonb;
BEGIN
    IF evaluation IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(hashtext('catalog-taxonomy:' || current_schema()));
    END IF;
    IF NOT pg_try_advisory_xact_lock(hashtext(current_schema()),
            hashtext('catalog-document-storage:' || document_key)) THEN
        RAISE EXCEPTION USING ERRCODE='MZ002',
            MESSAGE='Metadata evidence document is busy; retry after the current operation';
    END IF;
    SELECT to_jsonb(d) INTO old_document FROM catalog_documents d
        WHERE d.md5=document_key FOR UPDATE;
    IF old_document IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='MZ001', MESSAGE='Evidence document is missing; resume with a fresh inventory';
    END IF;
    SELECT to_jsonb(p) INTO old_publication FROM catalog_publications p
        WHERE p.publication_id=publication_key FOR UPDATE;
    IF old_publication IS NULL OR (old_document->>'publication_id')::bigint<>publication_key
            OR old_publication->>'merged_into_id' IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='MZ001', MESSAGE='Publication membership changed; resume with a fresh inventory';
    END IF;
    IF evaluation IS NULL AND (old_publication->>'metadata_present')::boolean THEN
        RAISE EXCEPTION USING ERRCODE='MZ001', MESSAGE='Publication already has metadata; extraction only fills missing metadata';
    END IF;
    IF evaluation IS NOT NULL AND NOT (old_publication->>'metadata_present')::boolean THEN
        RAISE EXCEPTION USING ERRCODE='MZ001', MESSAGE='Publication no longer has metadata; evaluation save rolled back';
    END IF;
    PERFORM location_id FROM catalog_locations WHERE md5=document_key ORDER BY location_id FOR UPDATE;
    SELECT to_jsonb(state) INTO fresh FROM ({{snapshot_query}}) state;
    IF fresh IS NULL OR jsonb_typeof(fresh->'sources') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE='MZ001', MESSAGE='Metadata source became ineligible; resume with a fresh inventory';
    END IF;
    IF fresh - 'sources' IS DISTINCT FROM request->'snapshot'->'publication'
            OR jsonb_array_length(fresh->'sources') IS DISTINCT FROM 1
            OR fresh->'sources'->0 IS DISTINCT FROM request->'snapshot'->'source' THEN
        RAISE EXCEPTION USING ERRCODE='MZ001',
            MESSAGE='Metadata source, revisions, protections or evidence changed; resume with a fresh inventory';
    END IF;
    IF EXISTS (SELECT 1 FROM catalog_proposals WHERE publication_id=publication_key
            AND kind='metadata' AND status IN ('pending','deferred')
            AND evidence->>'actor' IN ('library.metadata_extract','maintenance.monocorpus_meta_evaluate')) THEN
        RAISE EXCEPTION USING ERRCODE='MZ001', MESSAGE='Publication metadata awaits review; resume after review';
    END IF;
    expected_decision := jsonb_build_object('inclusion',old_document->'inclusion',
        'classification_id',old_publication->'classification_id','evaluation_method',old_document->'evaluation_method');

    IF evaluation IS NOT NULL AND (evaluation->>'applicable')::boolean THEN
        FOR taxonomy_label IN SELECT value FROM jsonb_array_elements_text(evaluation->'path') LOOP
            SELECT n.node_id INTO taxonomy_node FROM catalog_classification_nodes n
                WHERE n.ddc=evaluation->>'ddc' AND lower(n.label_en)=lower(taxonomy_label)
                    AND n.parent_id IS NOT DISTINCT FROM taxonomy_parent
                ORDER BY n.node_id LIMIT 1 FOR SHARE;
            IF taxonomy_node IS NULL THEN
                INSERT INTO catalog_classification_nodes(ddc,label_en,parent_id)
                    VALUES(evaluation->>'ddc',taxonomy_label,taxonomy_parent)
                    RETURNING node_id INTO taxonomy_node;
            END IF;
            taxonomy_parent := taxonomy_node;
        END LOOP;
        SELECT c.classification_id INTO classification_key FROM catalog_classifications c
            WHERE c.node_id=taxonomy_parent FOR SHARE;
        IF classification_key IS NULL THEN
            INSERT INTO catalog_classifications(node_id,status,created_by)
                VALUES(taxonomy_parent,'pending','gemini')
                RETURNING classification_id INTO classification_key;
        END IF;
        expected_path := catalog_path(taxonomy_parent);
        IF expected_path->>'ddc' IS DISTINCT FROM evaluation->>'ddc'
                OR (SELECT jsonb_agg(lower(value) ORDER BY ordinality)
                    FROM jsonb_array_elements_text(expected_path->'path_en') WITH ORDINALITY)
                    IS DISTINCT FROM (SELECT jsonb_agg(lower(value) ORDER BY ordinality)
                    FROM jsonb_array_elements_text(evaluation->'path') WITH ORDINALITY) THEN
            RAISE EXCEPTION USING ERRCODE='MZ001', MESSAGE='Classification path differs from the evaluated path; save rolled back';
        END IF;
    ELSIF evaluation IS NOT NULL THEN
        -- Rejecting one file must not erase shared bibliographic classification.
        classification_key := (old_publication->>'classification_id')::bigint;
    END IF;

    IF plan->'decision_proposal'<>'{}'::jsonb THEN
        INSERT INTO catalog_proposals(kind,publication_id,evidence,field_changes)
            VALUES('metadata',publication_key,jsonb_build_object('actor',actor_name,'md5',document_key,
                'publication_revision',old_publication->'revision','document_revision',old_document->'revision'),
                jsonb_build_object('document',plan->'decision_proposal')) RETURNING proposal_id INTO proposal_key;
        proposals := proposals || jsonb_build_array(proposal_key);
    END IF;
    IF plan->'decision'<>'{}'::jsonb THEN
        UPDATE catalog_documents SET inclusion='pending',evaluation_method=NULL,
            revision=revision+1,updated_at=clock_timestamp() WHERE md5=document_key;
        expected_decision := jsonb_build_object('inclusion','pending',
            'classification_id',old_publication->'classification_id','evaluation_method',NULL);
    END IF;
    IF plan->'blocked'<>'{}'::jsonb THEN
        INSERT INTO catalog_proposals(kind,publication_id,evidence,field_changes)
            VALUES('metadata',publication_key,jsonb_build_object('actor',actor_name,'md5',document_key,
                'publication_revision',old_publication->'revision'),plan->'blocked')
            RETURNING proposal_id INTO proposal_key;
        proposals := proposals || jsonb_build_array(proposal_key);
    END IF;

    IF (plan->>'languages_changed')::boolean THEN
        DELETE FROM catalog_publication_languages WHERE publication_id=publication_key;
        INSERT INTO catalog_publication_languages(publication_id,position,language)
            SELECT publication_key,(ordinality-1)::integer,value
            FROM jsonb_array_elements_text(desired->'languages') WITH ORDINALITY;
    END IF;
    IF plan->'relations' ? 'credits' THEN
        DELETE FROM catalog_contributions WHERE publication_id=publication_key
            AND contribution_id<>ALL(ARRAY(SELECT value::bigint FROM jsonb_array_elements_text(plan->'retained_credits')));
        DELETE FROM catalog_credit_groups g WHERE g.publication_id=publication_key AND NOT EXISTS (
            SELECT 1 FROM jsonb_array_elements(desired->'credits') c
            WHERE c->>'role'=g.role AND (c->>'position')::integer=g.position);
        INSERT INTO catalog_credit_groups(publication_id,role,position,role_name)
            SELECT DISTINCT publication_key,c->>'role',(c->>'position')::integer,c->>'role_name'
            FROM jsonb_array_elements(desired->'credits') c WHERE true
            ON CONFLICT(publication_id,role,position) DO UPDATE SET role_name=EXCLUDED.role_name;
        INSERT INTO catalog_names(kind,raw_name)
            SELECT DISTINCT c->>'kind',c->>'raw_name' FROM jsonb_array_elements(desired->'credits') c WHERE true
            ORDER BY 1,2
            ON CONFLICT(kind,raw_name) DO UPDATE SET raw_name=EXCLUDED.raw_name;
        INSERT INTO catalog_contributions(publication_id,name_id,role,position,nested_position)
            SELECT publication_key,n.name_id,c->>'role',(c->>'position')::integer,(c->>'nested_position')::integer
            FROM jsonb_array_elements(desired->'credits') c
            JOIN catalog_names n ON n.kind=c->>'kind' AND n.raw_name=c->>'raw_name'
            WHERE NOT EXISTS (SELECT 1 FROM catalog_contributions prior
                WHERE prior.publication_id=publication_key AND prior.role=c->>'role'
                AND prior.position=(c->>'position')::integer AND prior.nested_position=(c->>'nested_position')::integer);
    END IF;
    IF plan->'relations' ? 'identifiers' THEN
        DELETE FROM catalog_identifiers WHERE publication_id=publication_key AND kind='isbn';
        INSERT INTO catalog_identifiers(publication_id,kind,value,normalized,position)
            SELECT publication_key,'isbn',value,regexp_replace(upper(value),'[^0-9X]','','g'),(ordinality-1)::integer
            FROM jsonb_array_elements_text(desired->'identifiers') WITH ORDINALITY;
    END IF;
    IF plan->'relations' ? 'genres' THEN
        DELETE FROM catalog_genres WHERE publication_id=publication_key;
        INSERT INTO catalog_genres(publication_id,position,value)
            SELECT publication_key,(ordinality-1)::integer,value
            FROM jsonb_array_elements_text(desired->'genres') WITH ORDINALITY;
    END IF;
    IF plan->'relations' ? 'subjects' THEN
        DELETE FROM catalog_subjects WHERE publication_id=publication_key;
        INSERT INTO catalog_subjects(publication_id,position,name,term_code,set_name,set_url,set_is_url)
            SELECT publication_key,(ordinality-1)::integer,value->>'name',value->>'termCode',
                value->'inDefinedTermSet'->>'name',
                CASE WHEN jsonb_typeof(value->'inDefinedTermSet')='string' THEN value->>'inDefinedTermSet'
                     ELSE value->'inDefinedTermSet'->>'url' END,
                jsonb_typeof(value->'inDefinedTermSet')='string'
            FROM jsonb_array_elements(desired->'subjects') WITH ORDINALITY;
    END IF;
    IF plan->'relations' ? 'audiences' THEN
        DELETE FROM catalog_audiences WHERE publication_id=publication_key;
        INSERT INTO catalog_audiences(publication_id,position,kind,audience_type,min_age,max_age)
            SELECT publication_key,(ordinality-1)::integer,value->>'@type',value->>'audienceType',
                (value->>'suggestedMinAge')::integer,(value->>'suggestedMaxAge')::integer
            FROM jsonb_array_elements(desired->'audiences') WITH ORDINALITY;
    END IF;
    IF plan->'relations' ? 'based_on' THEN
        DELETE FROM catalog_references WHERE publication_id=publication_key;
        reference := desired->'based_on';
        IF reference<>'null'::jsonb THEN
            INSERT INTO catalog_references(publication_id,work_type,name,language,urls_present)
                VALUES(publication_key,reference->>'@type',reference->>'name',reference->>'inLanguage',reference ? 'url');
            INSERT INTO catalog_reference_urls(publication_id,position,url)
                SELECT publication_key,(ordinality-1)::integer,value
                FROM jsonb_array_elements_text(COALESCE(reference->'url','[]'::jsonb)) WITH ORDINALITY;
            INSERT INTO catalog_reference_authors(publication_id,position,kind,name)
                SELECT publication_key,(ordinality-1)::integer,value->>'@type',value->>'name'
                FROM jsonb_array_elements(CASE
                    WHEN reference->'author' IS NULL THEN '[]'::jsonb
                    WHEN jsonb_typeof(reference->'author')='array' THEN reference->'author'
                    ELSE jsonb_build_array(reference->'author') END) WITH ORDINALITY;
        END IF;
    END IF;
    IF plan->'file_changes' ? 'access_modes' THEN
        DELETE FROM catalog_document_access_modes WHERE md5=document_key;
        INSERT INTO catalog_document_access_modes(md5,position,mode)
            SELECT document_key,(ordinality-1)::integer,value
            FROM jsonb_array_elements_text(desired->'access_modes') WITH ORDINALITY;
    END IF;
    IF plan->'file_changes' ? 'sufficient_modes' THEN
        DELETE FROM catalog_sufficient_modes WHERE md5=document_key;
        INSERT INTO catalog_sufficient_modes(md5,position)
            SELECT document_key,(ordinality-1)::integer
            FROM jsonb_array_elements(desired->'sufficient_modes') WITH ORDINALITY;
        INSERT INTO catalog_sufficient_mode_items(md5,group_position,position,mode)
            SELECT document_key,(groups.ordinality-1)::integer,(children.ordinality-1)::integer,children.value
            FROM jsonb_array_elements(desired->'sufficient_modes') WITH ORDINALITY groups
            CROSS JOIN LATERAL jsonb_array_elements_text(groups.value) WITH ORDINALITY children;
    END IF;
    IF jsonb_array_length(plan->'file_changes')>0 THEN
        UPDATE catalog_documents SET revision=revision+1,updated_at=clock_timestamp()
            WHERE md5=document_key;
    END IF;
    INSERT INTO catalog_evidence(md5,record_kind,record_key,source,payload)
        VALUES(document_key,'publication',publication_key::text,actor_name,request->'schema_org');
    UPDATE catalog_publications SET
        work_type=desired->'scalars'->>'work_type', name=desired->'scalars'->>'name',
        description=desired->'scalars'->>'description', edition=desired->'scalars'->>'edition',
        date_published=desired->'scalars'->>'date_published', page_count=(desired->'scalars'->>'page_count')::integer,
        audience_array=(desired->>'audience_array')::boolean,
        has_metadata=true,metadata_present=true,revision=revision+1,updated_at=clock_timestamp()
        WHERE publication_id=publication_key RETURNING to_jsonb(catalog_publications) INTO new_publication;
    IF evaluation IS NULL THEN
        UPDATE catalog_documents SET meta_extraction_method=request->>'method',revision=revision+1,updated_at=clock_timestamp()
            WHERE md5=document_key;
    ELSE
        old_publication := new_publication;
        SELECT COALESCE(jsonb_object_agg(change.key,change.value),'{}'::jsonb) INTO decision_changes
            FROM jsonb_each(jsonb_build_object(
                'inclusion',CASE WHEN (evaluation->>'applicable')::boolean THEN 'included' ELSE 'excluded' END,
                'evaluation_method',request->>'method')) change
            WHERE old_document->change.key IS DISTINCT FROM change.value;
        IF decision_changes<>'{}'::jsonb
                OR old_publication->'classification_id' IS DISTINCT FROM coalesce(to_jsonb(classification_key),'null'::jsonb) THEN
            IF request->'snapshot'->'source'->'publication_protections' ? 'classification_id'
                    OR request->'snapshot'->'source'->'document_protections'
                        ?| ARRAY['inclusion','evaluation_method'] THEN
                INSERT INTO catalog_proposals(kind,publication_id,evidence,field_changes)
                    VALUES('metadata',publication_key,jsonb_build_object('actor',actor_name,'md5',document_key,
                        'publication_revision',old_publication->'revision',
                        'document_revision',request->'snapshot'->'source'->'revision'),
                        jsonb_build_object('document',decision_changes,'publication',
                            jsonb_build_object('classification_id',classification_key)))
                    RETURNING proposal_id INTO proposal_key;
                proposals := proposals || jsonb_build_array(proposal_key);
                decision_retained := true;
                expected_path := request->'expected_classification_path';
            ELSE
                UPDATE catalog_documents SET
                    inclusion=CASE WHEN (evaluation->>'applicable')::boolean THEN 'included' ELSE 'excluded' END,
                    evaluation_method=request->>'method',revision=revision+1,updated_at=clock_timestamp()
                    WHERE md5=document_key;
                UPDATE catalog_publications SET classification_id=classification_key,
                    revision=revision+1,updated_at=clock_timestamp()
                    WHERE publication_id=publication_key AND classification_id IS DISTINCT FROM classification_key;
            END IF;
        END IF;
        IF NOT decision_retained THEN
            expected_decision := jsonb_build_object(
                'inclusion',CASE WHEN (evaluation->>'applicable')::boolean THEN 'included' ELSE 'excluded' END,
                'classification_id',classification_key,'evaluation_method',request->>'method');
        END IF;
        INSERT INTO catalog_evidence(md5,record_kind,record_key,source,payload)
            VALUES(document_key,'document',document_key,actor_name,evaluation);
        validation_key := CASE WHEN decision_retained THEN 'retained' ELSE 'accepted' END;
        IF jsonb_array_length(request->'evaluation_validation'->validation_key)>0
                AND jsonb_array_length(proposals)=0 THEN
            RAISE EXCEPTION USING ERRCODE='MZ001', MESSAGE='Protected or retained metadata requires review; save rolled back';
        END IF;
    END IF;

    SELECT to_jsonb(state) INTO fresh FROM ({{snapshot_query}}) state;
    metadata_after := fresh->'sources'->0->'metadata_record';
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'role',c->'role','role_name',c->'role_name','position',c->'position','nested_position',c->'nested_position',
        'raw_name',c->'raw_name','kind',c->'kind') ORDER BY c->>'role',(c->>'position')::integer,(c->>'nested_position')::integer),
        '[]'::jsonb) INTO actual FROM jsonb_array_elements(metadata_after->'credits') c;
    actual := (metadata_after - 'publication_id' - 'revision') || jsonb_build_object('credits',actual);
    -- Parenthesize JSON paths before subtracting keys: - binds more tightly than ->.
    IF metadata_after IS NULL OR actual IS DISTINCT FROM desired
            OR fresh->'sources'->0->'classification_path' IS DISTINCT FROM expected_path
            OR jsonb_build_object('inclusion',fresh->'sources'->0->'inclusion','classification_id',fresh->'classification_id',
                'evaluation_method',fresh->'sources'->0->'evaluation_method') IS DISTINCT FROM expected_decision
            OR (evaluation IS NOT NULL AND fresh->'sources'->0->'meta_extraction_method'
                IS DISTINCT FROM request->'snapshot'->'source'->'meta_extraction_method')
            OR ((fresh->'sources'->0) - ARRAY['revision','updated_at','meta_extraction_method','publication_record','metadata_record','classification_path','inclusion','evaluation_method'])
                IS DISTINCT FROM ((request->'snapshot'->'source') - ARRAY['revision','updated_at','meta_extraction_method','publication_record','metadata_record','classification_path','inclusion','evaluation_method']) THEN
        RAISE EXCEPTION USING ERRCODE='MZ001', MESSAGE='Stored metadata differs from the validated result; save rolled back';
    END IF;
    PERFORM set_config('manzara.metadata_save_result',proposals::text,true);
END;
$manzara_metadata_save$;
SELECT current_setting('manzara.metadata_save_result')::jsonb;
