-- Inclusion is a publication decision; privacy belongs to each individual file.
-- Replacing the projection corrects existing rows without changing domain facts.
CREATE OR REPLACE VIEW public.metadata AS
SELECT d.md5,
       CASE WHEN p.metadata_present
            THEN "__CATALOG_SCHEMA__".catalog_schema_org(d.md5)::json
            ELSE NULL::json END AS schema_org,
       CASE
           WHEN d.restricted IS NOT FALSE THEN FALSE
           WHEN p.inclusion='included' THEN TRUE
           WHEN p.inclusion='excluded' THEN FALSE
           ELSE NULL::boolean
       END AS lib,
       p.evaluation_method AS lib_eval_method,
       p.classification_id
FROM "__CATALOG_SCHEMA__".catalog_document_metadata d
JOIN "__CATALOG_SCHEMA__".catalog_publication_metadata p USING(publication_id)
WHERE p.has_metadata;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_task_metadata_command()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO '__CATALOG_SCHEMA__', 'public', 'public'
AS $$
#variable_conflict use_column
<<catalog_task_metadata_command>>
DECLARE pub catalog_publication_metadata; changes JSONB='{}'; file_restricted BOOLEAN;
BEGIN
 IF current_setting('manzara.catalog_projection',true)='on' THEN RETURN coalesce(NEW,OLD); END IF;
 IF TG_OP='DELETE' THEN
   IF NOT EXISTS(SELECT 1 FROM catalog_document_metadata WHERE md5=OLD.md5) THEN RETURN OLD; END IF;
   RAISE EXCEPTION 'metadata deletion requires explicit catalog review';
 END IF;
 -- Lock file privacy before interpreting file-level lib, including INSERT commands.
 SELECT restricted INTO file_restricted FROM catalog_document_metadata WHERE md5=NEW.md5 FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'document missing'; END IF;
 IF file_restricted IS NOT FALSE AND NEW.lib IS TRUE THEN
   RAISE EXCEPTION 'restricted files cannot enter the online library; change publication inclusion through the publication record';
 END IF;
 SELECT p.* INTO pub FROM catalog_publication_metadata p JOIN catalog_document_metadata d USING(publication_id) WHERE d.md5=NEW.md5 FOR UPDATE OF p;
 IF pub.publication_id IS NULL THEN RAISE EXCEPTION 'document missing'; END IF;
 UPDATE catalog_publication_metadata SET has_metadata=true,metadata_present=NEW.schema_org IS NOT NULL WHERE publication_id=pub.publication_id;
 IF TG_OP='INSERT' OR NEW.schema_org::jsonb IS DISTINCT FROM OLD.schema_org::jsonb THEN
   PERFORM catalog_task_metadata(NEW.md5,NEW.schema_org::jsonb);
 END IF;
 -- A restricted file's false/null lib is a privacy result, not a decision about
 -- its publication or unrestricted siblings. Preserve publication inclusion.
 IF file_restricted IS FALSE AND (TG_OP='INSERT' OR NEW.lib IS DISTINCT FROM OLD.lib) THEN
   changes=changes||jsonb_build_object('inclusion',CASE NEW.lib WHEN TRUE THEN 'included' WHEN FALSE THEN 'excluded' ELSE 'pending' END);
 END IF;
 IF TG_OP='INSERT' OR NEW.lib_eval_method IS DISTINCT FROM OLD.lib_eval_method THEN changes=changes||jsonb_build_object('evaluation_method',NEW.lib_eval_method); END IF;
 IF TG_OP='INSERT' OR NEW.classification_id IS DISTINCT FROM OLD.classification_id THEN changes=changes||jsonb_build_object('classification_id',NEW.classification_id); END IF;
 PERFORM catalog_task_patch('publication',pub.publication_id::text,changes,NEW.md5);
 SELECT * INTO pub FROM catalog_publication_metadata WHERE publication_id=pub.publication_id;
 NEW.schema_org=catalog_schema_org(NEW.md5);
 -- RETURNING must use exactly the same privacy projection as subsequent reads.
 SELECT m.lib INTO NEW.lib FROM public.metadata m WHERE m.md5=NEW.md5;
 NEW.lib_eval_method=pub.evaluation_method;NEW.classification_id=pub.classification_id;
 RETURN NEW;
END $$;
