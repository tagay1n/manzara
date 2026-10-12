-- Remove application audit writes before dropping their storage.
-- Preserve adapter behavior, protection enforcement, and row revisions.

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__"."catalog_publisher_proposal_command"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public'
    AS $$
DECLARE old_row catalog_proposals; new_row catalog_proposals; data JSONB; changes JSONB;
        new_status TEXT; member TEXT; selected JSONB; proposal_key BIGINT;
BEGIN
 IF TG_OP<>'INSERT' THEN
   SELECT * INTO STRICT old_row FROM catalog_proposals WHERE proposal_id=OLD.proposal_id FOR UPDATE;
 END IF;
 IF TG_OP='DELETE' THEN
   IF old_row.status IN ('applied','separate') THEN RETURN OLD; END IF;
   UPDATE catalog_proposals SET status='rejected',revision=revision+1,updated_at=CURRENT_TIMESTAMP,
     field_changes=jsonb_set(field_changes,'{publisher}',coalesce(field_changes->'publisher','{}')||jsonb_build_object('discarded',true))
     WHERE proposal_id=OLD.proposal_id RETURNING * INTO new_row;
 ELSE
   IF NEW.status IS NULL THEN NEW.status='pending'; END IF;
   IF NEW.status NOT IN ('pending','staged','skipped','separate','applied','rejected') THEN RAISE EXCEPTION 'invalid publisher proposal state'; END IF;
   new_status=CASE NEW.status WHEN 'staged' THEN 'pending' WHEN 'skipped' THEN 'deferred' ELSE NEW.status END;
   IF TG_OP='UPDATE' AND NEW.proposal_id IS DISTINCT FROM OLD.proposal_id THEN RAISE EXCEPTION 'proposal identity is immutable'; END IF;
   IF TG_OP='UPDATE' AND old_row.status IN ('applied','separate','rejected') THEN RAISE EXCEPTION 'publisher decision is already final'; END IF;
   NEW.created_at=coalesce(NEW.created_at,CURRENT_TIMESTAMP::text);
   NEW.updated_at=coalesce(NEW.updated_at,CURRENT_TIMESTAMP::text);
   data=to_jsonb(NEW)-'proposal_id';
   selected=coalesce(nullif(NEW.review_edit,'null'::jsonb),NEW.proposal);
   IF TG_OP='INSERT' THEN
     INSERT INTO catalog_proposals(kind,status,display_name,evidence)
     VALUES('identity',new_status,coalesce(selected->>'display_name',selected->>'proposed_name'),
       jsonb_build_object('source','catalog.publisher_analysis','publisher',data)) RETURNING * INTO new_row;
     NEW.proposal_id=new_row.proposal_id;
   ELSE
     SELECT coalesce(jsonb_object_agg(key,value),'{}') INTO changes FROM jsonb_each(data) n
       WHERE value IS DISTINCT FROM (to_jsonb(OLD)->key);
     UPDATE catalog_proposals SET status=new_status,display_name=coalesce(selected->>'display_name',selected->>'proposed_name'),
       field_changes=jsonb_set(field_changes,'{publisher}',coalesce(field_changes->'publisher','{}')||changes),
       revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE proposal_id=OLD.proposal_id RETURNING * INTO new_row;
   END IF;
   proposal_key=new_row.proposal_id;
   IF TG_OP='INSERT' OR selected->'member_ids' IS DISTINCT FROM coalesce(nullif(OLD.review_edit,'null'::jsonb),OLD.proposal)->'member_ids' THEN
     IF jsonb_typeof(selected->'member_ids') IS DISTINCT FROM 'array' OR jsonb_array_length(selected->'member_ids') NOT BETWEEN 1 AND 200 THEN RAISE EXCEPTION 'bounded publisher members required'; END IF;
     DELETE FROM catalog_proposal_members WHERE proposal_id=proposal_key;
     FOR member IN SELECT jsonb_array_elements_text(selected->'member_ids') LOOP
       PERFORM catalog_review_member(proposal_key,member,'organization');
     END LOOP;
   END IF;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_patch"("kind" "text", "key" "text", "changes" "jsonb", "digest" "text" DEFAULT NULL::"text") RETURNS "void"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $_$
#variable_conflict use_column
<<catalog_task_patch>>
DECLARE relation TEXT; pk TEXT; before JSONB; allowed JSONB='{}'; blocked JSONB='{}';
        field TEXT; value JSONB; assignments TEXT; actor TEXT; proposal BIGINT;
BEGIN
 actor=coalesce(nullif(current_setting('manzara.catalog_actor',true),''),'task');
 CASE kind WHEN 'publication' THEN relation='catalog_publication_metadata';pk='publication_id';
   WHEN 'document' THEN relation='catalog_document_metadata';pk='md5';
   WHEN 'entity' THEN relation='catalog_entities';pk='entity_id';
   WHEN 'collection' THEN relation='catalog_collections';pk='collection_id';
   ELSE RAISE EXCEPTION 'unsupported task record kind'; END CASE;
 EXECUTE format('SELECT to_jsonb(r) FROM %I r WHERE %I::text=$1 FOR UPDATE',relation,pk) INTO before USING key;
 IF before IS NULL THEN RAISE EXCEPTION 'catalog % missing',kind; END IF;
 FOR field,value IN SELECT * FROM jsonb_each(changes) LOOP
   IF NOT before ? field OR field IN (pk,'revision','updated_at') THEN RAISE EXCEPTION 'unsupported task field %',field; END IF;
   IF before->field IS NOT DISTINCT FROM value THEN CONTINUE; END IF;
   IF actor='task' AND EXISTS(SELECT 1 FROM catalog_protections WHERE record_kind=kind AND record_key=key AND catalog_protections.field=catalog_task_patch.field) THEN
     blocked=blocked||jsonb_build_object(field,value);
   ELSE allowed=allowed||jsonb_build_object(field,value); END IF;
 END LOOP;
 IF blocked<>'{}' THEN
   IF kind='publication' THEN
     INSERT INTO catalog_proposals(kind,publication_id,evidence,field_changes) VALUES
       ('metadata',key::bigint,jsonb_build_object('md5',digest,'actor',actor,'publication_revision',before->'revision'),blocked);
   ELSIF kind='entity' THEN
     INSERT INTO catalog_proposals(kind,display_name,evidence) VALUES ('identity',coalesce(blocked->>'display_name',before->>'display_name'),
       jsonb_build_object('actor',actor,'field_changes',blocked)) RETURNING proposal_id INTO proposal;
     INSERT INTO catalog_proposal_members(proposal_id,entity_id,reviewed_revision,snapshot)
       VALUES(proposal,key::bigint,(before->>'revision')::bigint,before);
   ELSE RAISE EXCEPTION 'protected % fields changed; review the source before retry: %',kind,blocked; END IF;
 END IF;
 IF allowed='{}' THEN RETURN; END IF;
 SELECT string_agg(format('%I=v.%I',f,f),',') INTO assignments FROM jsonb_object_keys(allowed) f;
 EXECUTE format('UPDATE %I r SET %s,revision=r.revision+1,updated_at=CURRENT_TIMESTAMP FROM jsonb_populate_record(NULL::%I,$2) v WHERE r.%I::text=$1',relation,assignments,relation,pk)
 USING key,allowed;
 IF actor<>'task' THEN
   INSERT INTO catalog_protections(record_kind,record_key,field,actor) SELECT kind,key,f,actor FROM jsonb_object_keys(allowed) f
     ON CONFLICT(record_kind,record_key,field) DO UPDATE SET actor=EXCLUDED.actor;
 END IF;
END $_$;

-- These retired installers are absent from the retained baseline and could
-- reinstall the former audit-writing adapters on historical deployments.
DROP FUNCTION IF EXISTS "__CATALOG_SCHEMA__".catalog_install_lean_adapters(text);
DROP FUNCTION IF EXISTS "__CATALOG_SCHEMA__".catalog_activate_task_adapters(text);

DROP TABLE "__CATALOG_SCHEMA__".catalog_revisions;
DROP TABLE "__CATALOG_SCHEMA__".normalization_events;
DROP TABLE "__CATALOG_SCHEMA__".library_collection_events;
