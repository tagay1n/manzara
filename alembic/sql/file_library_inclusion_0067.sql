SET LOCAL lock_timeout = '10s';
LOCK TABLE "__CATALOG_SCHEMA__".catalog_documents,
           "__CATALOG_SCHEMA__".catalog_publications,
           "__CATALOG_SCHEMA__".catalog_protections,
           "__CATALOG_SCHEMA__".catalog_proposals IN ACCESS EXCLUSIVE MODE;

-- Do not silently reinterpret unresolved publication-level review decisions.
DO $$
BEGIN
 IF EXISTS (SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_proposals
     WHERE status IN ('pending','deferred')
       AND field_changes ?| ARRAY['inclusion','lib','evaluation_method']) THEN
   RAISE EXCEPTION 'Resolve pending inclusion/evaluation proposals before moving inclusion to files';
 END IF;
END $$;

DO $$
BEGIN
 IF EXISTS(SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_documents d
     JOIN "__CATALOG_SCHEMA__".catalog_publications p USING(publication_id)
     WHERE d.inclusion IS DISTINCT FROM CASE WHEN d.restricted THEN 'excluded' ELSE p.inclusion END
        OR d.evaluation_method IS DISTINCT FROM p.evaluation_method) THEN
   RAISE EXCEPTION 'File inclusion changed during backfill; pause writers and resume the migration';
 END IF;
END $$;

ALTER TABLE "__CATALOG_SCHEMA__".catalog_documents
 ALTER COLUMN inclusion SET NOT NULL,
 ALTER COLUMN inclusion SET DEFAULT 'pending';

ALTER TABLE "__CATALOG_SCHEMA__".catalog_documents
 ADD CONSTRAINT ck_catalog_document_inclusion CHECK (inclusion IN ('pending','included','excluded')),
 ADD CONSTRAINT ck_catalog_document_library_privacy CHECK (NOT restricted OR inclusion='excluded');

CREATE INDEX idx_catalog_documents_included_publication
 ON "__CATALOG_SCHEMA__".catalog_documents(publication_id) WHERE inclusion='included';

INSERT INTO "__CATALOG_SCHEMA__".catalog_protections(record_kind,record_key,field,actor)
SELECT 'document',d.md5,protection.field,protection.actor
FROM "__CATALOG_SCHEMA__".catalog_protections protection
JOIN "__CATALOG_SCHEMA__".catalog_documents d ON d.publication_id::text=protection.record_key
WHERE protection.record_kind='publication' AND protection.field IN ('inclusion','evaluation_method')
ON CONFLICT(record_kind,record_key,field) DO NOTHING;
DELETE FROM "__CATALOG_SCHEMA__".catalog_protections
WHERE record_kind='publication' AND field IN ('inclusion','evaluation_method');

-- Restricting an existing included file revokes only that file's inclusion.
-- Attempts to explicitly include an already restricted file are rejected.
CREATE FUNCTION "__CATALOG_SCHEMA__".catalog_document_library_privacy() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.restricted THEN
   IF NEW.inclusion='included' AND
       (TG_OP='INSERT' OR NEW.inclusion IS DISTINCT FROM OLD.inclusion OR OLD.restricted) THEN
     RAISE EXCEPTION 'Restricted files cannot be included in the library';
   END IF;
   IF NEW.inclusion IS DISTINCT FROM 'excluded' THEN
     NEW.inclusion='excluded';
     IF TG_OP='UPDATE' AND NEW.revision=OLD.revision THEN NEW.revision=OLD.revision+1; END IF;
     NEW.updated_at=CURRENT_TIMESTAMP;
   END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER catalog_document_library_privacy
 BEFORE INSERT OR UPDATE OF restricted,inclusion ON "__CATALOG_SCHEMA__".catalog_documents
 FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_document_library_privacy();

-- Append fields so dependent views retain their column identities and grants.
CREATE OR REPLACE VIEW "__CATALOG_SCHEMA__".catalog_document_metadata AS
SELECT d.md5,d.publication_id,d.mime_type,d.complete,d.restricted,d.selected,
       d.content_extraction_method,d.meta_extraction_method,
       ARRAY(SELECT a.mode FROM "__CATALOG_SCHEMA__".catalog_document_access_modes a
             WHERE a.md5=d.md5 ORDER BY a.position) AS access_modes,
       d.revision,d.updated_at,d.inclusion,d.evaluation_method
FROM "__CATALOG_SCHEMA__".catalog_documents d;

-- Legacy publication status is derived and read-only. Empty publications are
-- excluded; pending files matter only when no file is already included.
CREATE OR REPLACE VIEW "__CATALOG_SCHEMA__".catalog_publication_metadata AS
SELECT p.publication_id,p.name,p.work_type,p.description,p.edition,p.date_published,p.page_count,
       ARRAY(SELECT l.language FROM "__CATALOG_SCHEMA__".catalog_publication_languages l
             WHERE l.publication_id=p.publication_id ORDER BY l.position) AS languages,
       p.audience_array,
       CASE WHEN EXISTS(SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_documents d
                         WHERE d.publication_id=p.publication_id AND d.inclusion='included') THEN 'included'
            WHEN EXISTS(SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_documents d
                         WHERE d.publication_id=p.publication_id AND d.inclusion='pending') THEN 'pending'
            ELSE 'excluded' END AS inclusion,
       (SELECT d.evaluation_method FROM "__CATALOG_SCHEMA__".catalog_documents d
        WHERE d.publication_id=p.publication_id AND d.evaluation_method IS NOT NULL
        ORDER BY (d.inclusion='included') DESC,d.md5 LIMIT 1) AS evaluation_method,
       p.classification_id,p.collection_id,p.merged_into_id,p.revision,p.updated_at,
       p.collection_item_title,p.collection_created_at,p.collection_updated_at,p.has_metadata,p.metadata_present
FROM "__CATALOG_SCHEMA__".catalog_publications p;

CREATE OR REPLACE VIEW public.metadata AS
SELECT d.md5,
       CASE WHEN p.metadata_present THEN "__CATALOG_SCHEMA__".catalog_schema_org(d.md5)::json
            ELSE NULL::json END AS schema_org,
       CASE WHEN d.restricted IS NOT FALSE THEN false
            WHEN d.inclusion='included' THEN true
            WHEN d.inclusion='excluded' THEN false ELSE NULL::boolean END AS lib,
       d.evaluation_method AS lib_eval_method,p.classification_id
FROM "__CATALOG_SCHEMA__".catalog_document_metadata d
JOIN "__CATALOG_SCHEMA__".catalog_publication_metadata p USING(publication_id)
WHERE p.has_metadata;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_task_metadata_command() RETURNS trigger
LANGUAGE plpgsql SET search_path TO '__CATALOG_SCHEMA__','public' AS $$
#variable_conflict use_column
DECLARE pub catalog_publication_metadata; d catalog_document_metadata;
        file_changes jsonb='{}'; publication_changes jsonb='{}';
BEGIN
 IF current_setting('manzara.catalog_projection',true)='on' THEN RETURN coalesce(NEW,OLD); END IF;
 IF TG_OP='DELETE' THEN
   IF NOT EXISTS(SELECT 1 FROM catalog_document_metadata WHERE md5=OLD.md5) THEN RETURN OLD; END IF;
   RAISE EXCEPTION 'metadata deletion requires explicit catalog review';
 END IF;
 SELECT * INTO d FROM catalog_document_metadata WHERE md5=NEW.md5 FOR UPDATE;
 IF d.md5 IS NULL THEN RAISE EXCEPTION 'document missing'; END IF;
 IF d.restricted IS NOT FALSE AND NEW.lib IS TRUE THEN
   RAISE EXCEPTION 'restricted files cannot enter the online library';
 END IF;
 SELECT * INTO pub FROM catalog_publication_metadata WHERE publication_id=d.publication_id FOR UPDATE;
 UPDATE catalog_publications SET has_metadata=true,metadata_present=NEW.schema_org IS NOT NULL
 WHERE publication_id=d.publication_id;
 IF TG_OP='INSERT' OR NEW.schema_org::jsonb IS DISTINCT FROM OLD.schema_org::jsonb THEN
   PERFORM catalog_task_metadata(NEW.md5,NEW.schema_org::jsonb);
 END IF;
 IF TG_OP='INSERT' OR NEW.lib IS DISTINCT FROM OLD.lib THEN
   file_changes=file_changes||jsonb_build_object('inclusion',
     CASE WHEN d.restricted THEN 'excluded' WHEN NEW.lib IS TRUE THEN 'included'
          WHEN NEW.lib IS FALSE THEN 'excluded' ELSE 'pending' END);
 END IF;
 IF TG_OP='INSERT' OR NEW.lib_eval_method IS DISTINCT FROM OLD.lib_eval_method THEN
   file_changes=file_changes||jsonb_build_object('evaluation_method',NEW.lib_eval_method);
 END IF;
 IF TG_OP='INSERT' OR NEW.classification_id IS DISTINCT FROM OLD.classification_id THEN
   publication_changes=jsonb_build_object('classification_id',NEW.classification_id);
 END IF;
 PERFORM catalog_task_patch('document',NEW.md5,file_changes,NEW.md5);
 PERFORM catalog_task_patch('publication',d.publication_id::text,publication_changes,NEW.md5);
 SELECT * INTO NEW FROM public.metadata WHERE md5=NEW.md5;
 RETURN NEW;
END $$;

-- Guard the generic adapter too: derived publication values must not be written
-- or protected as though they were independent decisions.
CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_patch"("kind" "text", "key" "text", "changes" "jsonb", "digest" "text" DEFAULT NULL::"text") RETURNS "void"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $_$
#variable_conflict use_column
<<catalog_task_patch>>
DECLARE relation TEXT; pk TEXT; before JSONB; allowed JSONB='{}'; blocked JSONB='{}';
        field TEXT; value JSONB; assignments TEXT; actor TEXT; proposal BIGINT;
BEGIN
 IF kind='publication' AND changes ?| ARRAY['inclusion','evaluation_method'] THEN
   RAISE EXCEPTION 'Library inclusion and evaluation belong to files; update the document record';
 END IF;
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

ALTER TABLE "__CATALOG_SCHEMA__".catalog_publications
 DROP COLUMN inclusion, DROP COLUMN evaluation_method;
