SET LOCAL search_path="__CATALOG_SCHEMA__",public;
SET LOCAL lock_timeout='5s';

-- Semantic negative decisions and identity conflicts remain durable.
ALTER TABLE personality_normalization_checkpoints ADD COLUMN successful_model TEXT;
ALTER TABLE personality_normalization_checkpoints ADD COLUMN decision_evidence JSONB NOT NULL DEFAULT '{}';
UPDATE personality_normalization_checkpoints p SET
    successful_model=d.model,decision_evidence=d.payload
FROM (SELECT raw_name,key AS model,value AS payload FROM personality_normalization_checkpoints
      CROSS JOIN LATERAL jsonb_each(attempted_models) WHERE value->>'kind'='decision') d
WHERE p.raw_name=d.raw_name AND p.state IN ('not_person','unusable','retry_requested');
UPDATE personality_normalization_checkpoints p SET successful_model=r.successful_model
FROM catalog_alias_reviews r JOIN catalog_names n USING(name_id)
WHERE p.state='succeeded' AND p.raw_name=n.raw_name AND r.entity_type='personality'
  AND p.canonical_id=r.entity_id;
DELETE FROM personality_normalization_checkpoints
WHERE state NOT IN ('succeeded','not_person','unusable','retry_requested')
    AND NOT (state='failed' AND coalesce(failure_context,'') LIKE 'Multiple compatible identities;%');
ALTER TABLE personality_normalization_checkpoints RENAME COLUMN failure_context TO decision_reason;
ALTER TABLE personality_normalization_checkpoints DROP COLUMN attempted_models RESTRICT;
ALTER TABLE personality_normalization_checkpoints DROP COLUMN retryable RESTRICT;
ALTER TABLE personality_normalization_checkpoints ADD CONSTRAINT ck_personality_durable_state
    CHECK(state IN ('succeeded','not_person','unusable','retry_requested','failed'));

-- A completed output or verified byte fact survives regeneration failures.
ALTER TABLE library_non_pdf_extraction_state ADD COLUMN decision_reason TEXT;
UPDATE library_non_pdf_extraction_state SET decision_reason=error_text WHERE status='unsupported';
DELETE FROM library_non_pdf_extraction_state WHERE status NOT IN ('ready','unsupported') AND verified_source_mime IS NULL;
ALTER TABLE library_non_pdf_extraction_state DROP CONSTRAINT IF EXISTS ck_library_non_pdf_extraction_status;
ALTER TABLE library_non_pdf_extraction_state DROP CONSTRAINT IF EXISTS ck_library_non_pdf_extraction_state_status;
UPDATE library_non_pdf_extraction_state SET status='detected' WHERE status NOT IN ('ready','unsupported');
ALTER TABLE library_non_pdf_extraction_state ADD CONSTRAINT ck_non_pdf_durable_state CHECK(status IN ('ready','unsupported','detected'));
ALTER TABLE library_non_pdf_extraction_state DROP COLUMN attempt_count RESTRICT;
ALTER TABLE library_non_pdf_extraction_state DROP COLUMN last_run_id RESTRICT;
ALTER TABLE library_non_pdf_extraction_state DROP COLUMN error_text RESTRICT;
ALTER TABLE document_cleanup_queue DROP COLUMN attempts RESTRICT;
ALTER TABLE document_cleanup_queue DROP COLUMN run_id RESTRICT;
ALTER TABLE document_cleanup_queue DROP COLUMN last_error RESTRICT;

-- Unknown privacy and public-asset invalidation are domain evidence, not retry logs.
INSERT INTO catalog_evidence(md5,record_kind,record_key,source,payload)
SELECT md5,'preview',request_id::text,'legacy.preview-access',jsonb_build_object('reason',error)
FROM catalog_preview_requests WHERE error='Legacy public assets require regeneration into private storage';
ALTER TABLE catalog_preview_requests DROP COLUMN error RESTRICT;

-- Stop projecting catalog decisions into a second physical publisher owner.
CREATE OR REPLACE FUNCTION catalog_project_review_decision() RETURNS TRIGGER LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__",public AS $fn$
DECLARE previous TEXT; id BIGINT;
BEGIN
 IF NEW.status=OLD.status OR current_setting('manzara.catalog_projection',true)='on' THEN RETURN NEW; END IF;
 IF NEW.evidence->>'source'<>'legacy.normalization_suggestions' THEN RETURN NEW; END IF;
 previous=coalesce(current_setting('manzara.catalog_projection',true),'');
 PERFORM set_config('manzara.catalog_projection','on',true);
 id=(NEW.evidence->>'legacy_id')::bigint;
 UPDATE normalization_suggestions SET status=CASE NEW.status WHEN 'applied' THEN 'accepted'
   WHEN 'separate' THEN 'dismissed' WHEN 'rejected' THEN 'dismissed' ELSE 'open' END,
   updated_at=CURRENT_TIMESTAMP::text WHERE suggestion_id=id;
 PERFORM set_config('manzara.catalog_projection',previous,true);
 RETURN NEW;
END $fn$;

-- Drafts now refer to actual catalog proposal identities.
UPDATE publisher_review_draft d SET proposal_ids=coalesce((
 SELECT jsonb_agg(c.proposal_id ORDER BY v.position)
 FROM jsonb_array_elements_text(d.proposal_ids) WITH ORDINALITY v(id,position)
 JOIN catalog_proposals c ON c.evidence->>'source'='legacy.publisher_merge_proposals'
   AND c.evidence->>'legacy_id'=v.id),'[]'::jsonb);

DROP TABLE library_collection_document_features RESTRICT;
DROP TABLE library_metadata_quality_state RESTRICT;
DROP TABLE library_collection_validation_attempts RESTRICT;
DROP TABLE library_book_previews RESTRICT;
DROP TABLE publisher_merge_proposals RESTRICT;
DROP FUNCTION catalog_task_preview_command() RESTRICT;

-- A projection stores no duplicate rows; mutations lock/update catalog truth.
CREATE VIEW catalog_publisher_proposals AS
SELECT c.proposal_id,(v.payload->>'analysis_id')::bigint AS analysis_id,
 v.payload->>'fingerprint' AS fingerprint,v.payload->'proposal' AS proposal,v.payload->'members' AS members,
 CASE c.status WHEN 'pending' THEN coalesce(v.payload->>'status','pending')
   WHEN 'deferred' THEN 'skipped' ELSE c.status END AS status,
 v.payload->>'created_at' AS created_at,v.payload->>'updated_at' AS updated_at,v.payload->'review_edit' AS review_edit
FROM catalog_proposals c CROSS JOIN LATERAL (
 SELECT coalesce(c.evidence->'publisher',c.evidence->'original','{}'::jsonb)
     ||coalesce(c.field_changes->'publisher','{}'::jsonb) AS payload
) v
WHERE c.evidence->>'source' IN ('legacy.publisher_merge_proposals','catalog.publisher_analysis')
  AND (c.field_changes->'publisher'->>'discarded') IS DISTINCT FROM 'true';

CREATE FUNCTION catalog_publisher_proposal_command() RETURNS TRIGGER LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__",public AS $fn$
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
 INSERT INTO catalog_revisions(record_kind,record_key,actor,"before","after")
 VALUES('proposal',new_row.proposal_id::text,coalesce(nullif(current_setting('manzara.catalog_actor',true),''),'owner'),
   CASE WHEN TG_OP='INSERT' THEN NULL ELSE to_jsonb(old_row) END,to_jsonb(new_row));
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $fn$;
CREATE TRIGGER catalog_publisher_proposal_write INSTEAD OF INSERT OR UPDATE OR DELETE
ON catalog_publisher_proposals FOR EACH ROW EXECUTE FUNCTION catalog_publisher_proposal_command();

CREATE VIEW catalog_selected_previews AS
SELECT r.md5,r.request_id,r.recipe AS recipe_version,r.status,r.source_page_count,r.private,
 (SELECT page_number FROM catalog_preview_pages p WHERE p.request_id=r.request_id AND p.role='first') AS first_preview_page,
 (SELECT page_number FROM catalog_preview_pages p WHERE p.request_id=r.request_id AND p.role='second') AS second_preview_page,
 (SELECT page_number FROM catalog_preview_pages p WHERE p.request_id=r.request_id AND p.role='last') AS last_preview_page
FROM catalog_preview_requests r JOIN catalog_documents d USING(md5)
WHERE r.status='ready' AND (NOT d.restricted OR r.private)
AND NOT EXISTS(SELECT 1 FROM catalog_preview_requests newer WHERE newer.md5=r.md5 AND newer.status='ready'
    AND (NOT d.restricted OR newer.private) AND newer.request_id>r.request_id);
