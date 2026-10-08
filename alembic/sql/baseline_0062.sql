-- Frozen durable schema at revision 20261008_0062 (PostgreSQL 18, UTF-8).
-- No corpus rows, local operational state, or historical migration installers.
-- The Alembic environment creates schemas and owns its version table.
DO $baseline_guard$
BEGIN
    IF current_setting('server_version_num')::integer < 180000
       OR current_setting('server_encoding') <> 'UTF8' THEN
        RAISE EXCEPTION 'Manzara requires PostgreSQL 18 or newer with UTF-8 encoding';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='__CATALOG_SCHEMA__' AND c.relkind IN ('r','p','v','m','S','f')
          AND c.relname <> 'alembic_version_manzara'
    ) OR EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='__CATALOG_SCHEMA__'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass
                          AND d.objid=p.oid AND d.deptype='e')
    ) OR EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname IN ('document','metadata','classification','isbn_keep_many')
    ) THEN
        RAISE EXCEPTION 'Baseline requires an empty catalog schema and no public domain relations; do not stamp an older database. See docs/operations.md';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
               WHERE e.extname='pg_trgm' AND n.nspname <> 'public') THEN
        RAISE EXCEPTION 'Manzara requires pg_trgm in the public schema';
    END IF;
END
$baseline_guard$;

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
SET LOCAL check_function_bodies = false;
SET LOCAL search_path = pg_catalog, public;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_alias_review_sync"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public'
    AS $$
DECLARE observed BIGINT;
BEGIN
 IF pg_trigger_depth()>1 THEN RETURN coalesce(NEW,OLD); END IF;
 IF TG_TABLE_NAME='catalog_entities' THEN
   FOR observed IN SELECT DISTINCT name_id FROM catalog_aliases WHERE entity_id=NEW.entity_id LOOP
     PERFORM catalog_refresh_alias_review(observed);
   END LOOP;
 ELSE
   IF TG_OP<>'INSERT' THEN PERFORM catalog_refresh_alias_review(OLD.name_id); END IF;
   IF TG_OP<>'DELETE' THEN PERFORM catalog_refresh_alias_review(NEW.name_id); END IF;
 END IF;
 RETURN coalesce(NEW,OLD);
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_array"("value" "jsonb") RETURNS "jsonb"
    LANGUAGE "sql" IMMUTABLE
    AS $$
 SELECT CASE WHEN value IS NULL OR value='null'::jsonb THEN '[]'::jsonb
             WHEN jsonb_typeof(value)='array' THEN value ELSE jsonb_build_array(value) END
$$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_command_snapshot_guard"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $_$
DECLARE key_column TEXT; root REGCLASS; root_key TEXT; previous JSONB; current_row JSONB; key_value TEXT;
BEGIN
 CASE TG_TABLE_NAME
 WHEN 'document' THEN key_column='md5';root='catalog_document_metadata';root_key='md5';
 WHEN 'metadata' THEN key_column='md5';root='catalog_document_metadata';root_key='md5';
 WHEN 'classification' THEN key_column='id';root='catalog_classifications';root_key='classification_id';
 WHEN 'normalization_canonicals' THEN key_column='canonical_id';root='catalog_entities';root_key='entity_id';
 WHEN 'normalization_aliases' THEN key_column='alias_id';root='catalog_alias_reviews';root_key='alias_id';
 WHEN 'library_collections' THEN key_column='collection_id';root='catalog_collections';root_key='collection_id';
 WHEN 'library_collection_items' THEN key_column='md5';root='catalog_document_metadata';root_key='md5';
 ELSE RAISE EXCEPTION 'unknown catalog command view'; END CASE;
 previous=to_jsonb(OLD);key_value=previous->>key_column;
 IF TG_OP='UPDATE' AND to_jsonb(NEW)->>key_column IS DISTINCT FROM key_value THEN
   RAISE EXCEPTION 'catalog command identity is immutable';
 END IF;
 EXECUTE format('SELECT to_jsonb(r) FROM %s r WHERE %I::text=$1 FOR UPDATE',root,root_key) USING key_value;
 IF TG_TABLE_NAME IN ('document','metadata','library_collection_items') THEN
   PERFORM 1 FROM catalog_publication_metadata WHERE publication_id=(SELECT publication_id FROM catalog_document_metadata WHERE md5=key_value) FOR UPDATE;
 END IF;
 EXECUTE format('SELECT to_jsonb(r) FROM %I.%I r WHERE %I::text=$1',TG_TABLE_SCHEMA,TG_TABLE_NAME,key_column) INTO current_row USING key_value;
 IF current_row IS DISTINCT FROM previous THEN
   RAISE EXCEPTION 'catalog record changed; reload before retrying' USING ERRCODE='40001';
 END IF;
 RETURN coalesce(NEW,OLD);
END $_$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_derive_keys"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
 CASE TG_TABLE_NAME
 WHEN 'catalog_entities' THEN NEW.normalized_name="__CATALOG_SCHEMA__".catalog_name_key(NEW.display_name);
 WHEN 'catalog_collections' THEN NEW.normalized_title="__CATALOG_SCHEMA__".catalog_title_key(NEW.title);
 WHEN 'catalog_identifiers' THEN NEW.normalized="__CATALOG_SCHEMA__".catalog_identifier_key(NEW.kind,NEW.value);
 ELSE RAISE EXCEPTION 'unsupported derived-key relation';
 END CASE;
 RETURN NEW;
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_external_review_key"("key" "text") RETURNS "text"
    LANGUAGE "sql" STABLE
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $$
 SELECT CASE WHEN key LIKE 'entity:%' THEN 'canonical:'||substring(key FROM 8)
   ELSE (SELECT 'raw:'||raw_name FROM catalog_names WHERE name_id=substring(key FROM 6)::bigint) END
$$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_identifier_key"("kind" "text", "value" "text") RETURNS "text"
    LANGUAGE "sql" IMMUTABLE STRICT PARALLEL SAFE
    AS $$
 SELECT CASE WHEN kind='isbn' THEN regexp_replace(upper(value COLLATE pg_catalog.pg_unicode_fast),'[^0-9X]','','g') ELSE value END
$$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_internal_review_key"("key" "text") RETURNS "text"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $$
DECLARE id BIGINT; name_kind TEXT;
BEGIN
 IF key LIKE 'canonical:%' THEN RETURN 'entity:'||substring(key FROM 11); END IF;
 IF key NOT LIKE 'raw:%' OR length(key)<=4 THEN RAISE EXCEPTION 'unsupported separation key'; END IF;
 name_kind=catalog_publisher_name_kind(substring(key FROM 5));
 INSERT INTO catalog_names(kind,raw_name) VALUES(name_kind,substring(key FROM 5))
   ON CONFLICT(kind,raw_name) DO UPDATE SET raw_name=EXCLUDED.raw_name RETURNING name_id INTO id;
 RETURN 'name:'||id;
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_lock_topology"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('catalog-topology:'||TG_TABLE_SCHEMA||':'||TG_TABLE_NAME,0));
 RETURN NULL;
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_name_key"("value" "text") RETURNS "text"
    LANGUAGE "sql" IMMUTABLE STRICT PARALLEL SAFE
    AS $$
 SELECT casefold(value COLLATE pg_catalog.pg_unicode_fast)
$$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_path"("id" bigint) RETURNS "jsonb"
    LANGUAGE "sql" STABLE
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $$
 WITH RECURSIVE path AS (
   SELECT n.*,0 AS depth FROM catalog_classification_nodes n WHERE node_id=id
   UNION ALL SELECT n.*,p.depth+1 FROM catalog_classification_nodes n JOIN path p ON n.node_id=p.parent_id
 ) SELECT jsonb_build_object('ddc',max(ddc),'path_en',jsonb_agg(label_en ORDER BY depth DESC),
     'path_tt',CASE WHEN bool_and(label_tt IS NOT NULL) THEN jsonb_agg(label_tt ORDER BY depth DESC) END) FROM path
$$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_project_review_decision"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public'
    AS $$
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
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_publisher_name_kind"("raw" "text") RETURNS "text"
    LANGUAGE "plpgsql" STABLE
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $$
DECLARE kinds TEXT[];
BEGIN
 SELECT array_agg(DISTINCT n.kind) INTO kinds FROM catalog_names n
   JOIN catalog_contribution_metadata c USING(name_id) WHERE n.raw_name=raw AND c.role='publisher';
 IF coalesce(cardinality(kinds),0)=0 THEN
   SELECT array_agg(DISTINCT n.kind) INTO kinds FROM catalog_names n
     JOIN catalog_aliases a USING(name_id) JOIN catalog_entity_roles r USING(entity_id)
     WHERE n.raw_name=raw AND r.role='publisher';
 END IF;
 IF cardinality(kinds)>1 THEN RAISE EXCEPTION 'ambiguous publisher name type; review in catalog'; END IF;
 RETURN coalesce(kinds[1],'organization');
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_publisher_proposal_command"() RETURNS "trigger"
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
 INSERT INTO catalog_revisions(record_kind,record_key,actor,"before","after")
 VALUES('proposal',new_row.proposal_id::text,coalesce(nullif(current_setting('manzara.catalog_actor',true),''),'owner'),
   CASE WHEN TG_OP='INSERT' THEN NULL ELSE to_jsonb(old_row) END,to_jsonb(new_row));
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_refresh_alias_review"("observed" bigint) RETURNS "void"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public'
    AS $$
DECLARE targets BIGINT[]; role TEXT; raw TEXT; kind TEXT;
BEGIN
 SELECT n.raw_name,n.kind INTO raw,kind FROM catalog_names n WHERE name_id=observed;
 IF raw IS NULL THEN RETURN; END IF;
 SELECT array_agg(DISTINCT a.entity_id) INTO targets FROM catalog_aliases a JOIN catalog_entities e USING(entity_id)
 WHERE a.name_id=observed AND e.status='active';
 role=CASE WHEN kind='organization' OR EXISTS(SELECT 1 FROM catalog_aliases a JOIN catalog_entity_roles r USING(entity_id)
   WHERE a.name_id=observed AND r.role='publisher') OR EXISTS(SELECT 1 FROM catalog_aliases a JOIN catalog_contribution_metadata c USING(entity_id) WHERE a.name_id=observed AND c.role='publisher') THEN 'publisher' ELSE 'personality' END;
 IF NOT EXISTS(SELECT 1 FROM catalog_alias_reviews WHERE name_id=observed) THEN
   INSERT INTO catalog_alias_reviews(name_id,entity_type,normalized_name,script_label,source,created_at)
   VALUES(observed,role,lower(raw),'other','catalog.owner',CURRENT_TIMESTAMP::text);
 END IF;
 UPDATE catalog_alias_reviews SET entity_id=CASE WHEN cardinality(targets)=1 THEN targets[1] END,
   decision_status=CASE WHEN cardinality(targets)=1 THEN 'linked' ELSE 'pending' END,updated_at=CURRENT_TIMESTAMP::text
 WHERE name_id=observed;
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_review_member"("proposal" bigint, "key" "text", "kind" "text") RETURNS "void"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $$
#variable_conflict use_column
DECLARE e catalog_entities; name_key BIGINT; visited BIGINT[]='{}';
BEGIN
 IF key LIKE 'canonical:%' THEN
   SELECT * INTO e FROM catalog_entities WHERE entity_id=substring(key FROM 11)::bigint;
   WHILE e.merged_into_id IS NOT NULL LOOP
     IF e.entity_id=ANY(visited) THEN RAISE EXCEPTION 'identity merge cycle'; END IF;
     visited=array_append(visited,e.entity_id);
     SELECT * INTO e FROM catalog_entities WHERE entity_id=e.merged_into_id;
   END LOOP;
   IF e.entity_id IS NULL OR e.status<>'active' THEN RAISE EXCEPTION 'missing active review identity'; END IF;
   IF NOT EXISTS(SELECT 1 FROM catalog_proposal_members WHERE proposal_id=proposal AND entity_id=e.entity_id) THEN
     INSERT INTO catalog_proposal_members(proposal_id,entity_id,reviewed_revision,snapshot) VALUES(proposal,e.entity_id,e.revision,to_jsonb(e));
   END IF;
 ELSIF key LIKE 'raw:%' AND length(key)>4 THEN
   IF kind='organization' THEN
     kind=catalog_publisher_name_kind(substring(key FROM 5));
   END IF;
   INSERT INTO catalog_names(kind,raw_name) VALUES(kind,substring(key FROM 5)) ON CONFLICT(kind,raw_name) DO UPDATE SET raw_name=EXCLUDED.raw_name RETURNING name_id INTO name_key;
   IF NOT EXISTS(SELECT 1 FROM catalog_proposal_members WHERE proposal_id=proposal AND name_id=name_key) THEN
     INSERT INTO catalog_proposal_members(proposal_id,name_id,snapshot) VALUES(proposal,name_key,jsonb_build_object('kind',kind,'raw_name',substring(key FROM 5)));
   END IF;
 ELSE RAISE EXCEPTION 'unsupported review member'; END IF;
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_schema_org"("digest" "text") RETURNS "jsonb"
    LANGUAGE "plpgsql" STABLE
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $$
#variable_conflict use_column
<<catalog_array>>
DECLARE d catalog_document_metadata; p catalog_publication_metadata; result JSONB; value JSONB; credit RECORD;
        taxonomy JSONB; node BIGINT;
BEGIN
 SELECT * INTO d FROM catalog_document_metadata WHERE md5=digest;
 SELECT * INTO p FROM catalog_publication_metadata WHERE publication_id=d.publication_id;
 IF p.publication_id IS NULL THEN RETURN NULL; END IF;
 result=jsonb_strip_nulls(jsonb_build_object('@context','https://schema.org','@type',p.work_type,
   'name',p.name,'description',p.description,'bookEdition',p.edition,'datePublished',p.date_published,
   'numberOfPages',p.page_count));
 IF cardinality(p.languages)>0 THEN result=result||jsonb_build_object('inLanguage',array_to_string(p.languages,',')); END IF;
 FOR credit IN
   SELECT c.role,c.position,c.role_name,jsonb_agg(jsonb_build_object('@type',initcap(n.kind),
      'name',coalesce(e.display_name,n.raw_name)) ORDER BY c.nested_position) AS people
   FROM catalog_contribution_metadata c JOIN catalog_names n USING(name_id) LEFT JOIN catalog_entities e USING(entity_id)
   WHERE c.publication_id=p.publication_id GROUP BY c.role,c.position,c.role_name ORDER BY c.role,c.position
 LOOP
   value=CASE WHEN jsonb_array_length(credit.people)=1 THEN credit.people->0 ELSE credit.people END;
   IF coalesce(credit.role_name,'')<>'' THEN value=jsonb_build_object('@type','Role','roleName',credit.role_name,'contributor',value); END IF;
   IF credit.role='publisher' THEN result=result||jsonb_build_object('publisher',value);
   ELSE result=jsonb_set(result,ARRAY[credit.role],coalesce(result->credit.role,'[]')||jsonb_build_array(value)); END IF;
 END LOOP;
 SELECT jsonb_agg(value ORDER BY position) INTO value FROM catalog_identifiers WHERE publication_id=p.publication_id;
 IF value IS NOT NULL THEN result=result||jsonb_build_object('isbn',value); END IF;
 SELECT jsonb_agg(value ORDER BY position) INTO value FROM catalog_genres WHERE publication_id=p.publication_id;
 IF value IS NOT NULL THEN result=result||jsonb_build_object('genre',value); END IF;
 SELECT jsonb_agg(jsonb_strip_nulls(jsonb_build_object('@type','DefinedTerm','name',name,'termCode',term_code,
   'inDefinedTermSet',CASE WHEN set_is_url THEN to_jsonb(set_url) ELSE
   jsonb_strip_nulls(jsonb_build_object('@type','DefinedTermSet','name',set_name,'url',set_url)) END)) ORDER BY position)
 INTO value FROM catalog_subjects WHERE publication_id=p.publication_id
   AND (p.classification_id IS NULL OR lower(coalesce(set_name,'')) NOT IN ('ddc','categorypath'));
 IF p.classification_id IS NOT NULL THEN
   SELECT node_id INTO node FROM catalog_classifications WHERE classification_id=p.classification_id;
   taxonomy=catalog_path(node);
   value=coalesce(value,'[]')||jsonb_build_array(
     jsonb_build_object('@type','DefinedTerm','termCode',taxonomy->>'ddc','inDefinedTermSet',jsonb_build_object('@type','DefinedTermSet','name','DDC')),
     jsonb_build_object('@type','DefinedTerm','termCode',(SELECT string_agg(x,' > ') FROM jsonb_array_elements_text(taxonomy->'path_en') x),
       'inDefinedTermSet',jsonb_build_object('@type','DefinedTermSet','name','CategoryPath')));
 END IF;
 IF value IS NOT NULL THEN result=result||jsonb_build_object('about',value); END IF;
 SELECT jsonb_agg(jsonb_strip_nulls(jsonb_build_object('@type',kind,'audienceType',audience_type,
   'suggestedMinAge',min_age,'suggestedMaxAge',max_age)) ORDER BY position) INTO value
 FROM catalog_audiences WHERE publication_id=p.publication_id;
 IF value IS NOT NULL THEN result=result||jsonb_build_object('audience',CASE WHEN p.audience_array THEN value ELSE value->0 END); END IF;
 IF cardinality(d.access_modes)>0 THEN result=result||jsonb_build_object('accessMode',to_jsonb(d.access_modes)); END IF;
 SELECT jsonb_agg(jsonb_build_object('@type','ItemList','itemListElement',modes) ORDER BY position) INTO value
 FROM catalog_sufficient_mode_metadata WHERE md5=digest;
 IF value IS NOT NULL THEN result=result||jsonb_build_object('accessModeSufficient',value); END IF;
 SELECT jsonb_strip_nulls(jsonb_build_object('@type',work_type,'name',name,'inLanguage',language,'url',urls)) INTO value
 FROM catalog_reference_metadata WHERE publication_id=p.publication_id;
 IF value IS NOT NULL THEN
   SELECT jsonb_agg(jsonb_build_object('@type',kind,'name',name) ORDER BY position) INTO taxonomy
   FROM catalog_reference_authors WHERE publication_id=p.publication_id;
   IF taxonomy IS NOT NULL THEN value=value||jsonb_build_object('author',taxonomy); END IF;
   result=result||jsonb_build_object('isBasedOn',value);
 END IF;
 RETURN result;
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_separation_command"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $$
#variable_conflict use_variable
DECLARE left_key TEXT;right_key TEXT;previous TEXT;
BEGIN
 IF current_setting('manzara.catalog_projection',true)='on' THEN RETURN coalesce(NEW,OLD); END IF;
 previous=coalesce(current_setting('manzara.catalog_projection',true),'');
 PERFORM set_config('manzara.catalog_projection','on',true);
 IF TG_TABLE_NAME='catalog_separations' THEN
   left_key=catalog_external_review_key(CASE WHEN TG_OP='DELETE' THEN OLD.left_key ELSE NEW.left_key END);
   right_key=catalog_external_review_key(CASE WHEN TG_OP='DELETE' THEN OLD.right_key ELSE NEW.right_key END);
   IF TG_OP='DELETE' THEN DELETE FROM publisher_separations WHERE publisher_separations.left_key=least(left_key,right_key) AND publisher_separations.right_key=greatest(left_key,right_key);
   ELSIF left_key<>right_key THEN
     INSERT INTO publisher_separations(left_key,right_key,provenance,created_at)
       VALUES(least(left_key,right_key),greatest(left_key,right_key),jsonb_build_object('source','catalog','actor',NEW.actor),CURRENT_TIMESTAMP::text)
       ON CONFLICT DO NOTHING;
   END IF;
 ELSE
   left_key=catalog_internal_review_key(CASE WHEN TG_OP='DELETE' THEN OLD.left_key ELSE NEW.left_key END);
   right_key=catalog_internal_review_key(CASE WHEN TG_OP='DELETE' THEN OLD.right_key ELSE NEW.right_key END);
   IF TG_OP='DELETE' THEN DELETE FROM catalog_separations WHERE catalog_separations.left_key=least(left_key,right_key) AND catalog_separations.right_key=greatest(left_key,right_key);
   ELSIF left_key<>right_key THEN INSERT INTO catalog_separations(left_key,right_key,actor) VALUES(least(left_key,right_key),greatest(left_key,right_key),'owner') ON CONFLICT DO NOTHING; END IF;
 END IF;
 PERFORM set_config('manzara.catalog_projection',previous,true);
 RETURN coalesce(NEW,OLD);
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_alias_command"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $_$
#variable_conflict use_column
DECLARE name_key BIGINT; kind TEXT; actor TEXT; id BIGINT; values_json JSONB; assignments TEXT; affected BIGINT;
BEGIN
 IF current_setting('manzara.catalog_projection',true)='on' THEN RETURN coalesce(NEW,OLD); END IF;
 actor=coalesce(nullif(current_setting('manzara.catalog_actor',true),''),'task');
 IF TG_OP='DELETE' THEN
   DELETE FROM catalog_aliases a USING catalog_names n WHERE a.name_id=n.name_id AND n.raw_name=OLD.raw_name AND a.entity_id=OLD.canonical_id;
   DELETE FROM catalog_alias_reviews WHERE alias_id=OLD.alias_id; RETURN OLD;
 END IF;
 IF NEW.decision_status='linked' AND NEW.canonical_id IS NOT NULL THEN
   SELECT e.kind,e.entity_id INTO kind,id FROM catalog_entities e WHERE entity_id=NEW.canonical_id AND status='active';
   IF id IS NULL THEN RAISE EXCEPTION 'alias target must be active'; END IF;
   INSERT INTO catalog_names(kind,raw_name) VALUES(kind,NEW.raw_name) ON CONFLICT(kind,raw_name) DO UPDATE SET raw_name=EXCLUDED.raw_name RETURNING name_id INTO name_key;
   INSERT INTO catalog_aliases(name_id,entity_id,approval) VALUES(name_key,id,CASE WHEN actor='task' THEN 'unconfirmed' ELSE 'confirmed' END)
     ON CONFLICT(name_id,entity_id) DO NOTHING;
   -- Retain a second hypothesis when a spelling is already used elsewhere.
   -- The unique legacy row is an operational summary, not a mention resolver.
   IF (SELECT count(*) FROM catalog_aliases a JOIN catalog_entities e USING(entity_id) WHERE name_id=name_key AND e.status='active')>1 THEN
     NEW.canonical_id=NULL;NEW.decision_status='pending';
   END IF;
 END IF;

 IF name_key IS NULL THEN
   kind=CASE NEW.entity_type WHEN 'personality' THEN 'person' ELSE 'organization' END;
   INSERT INTO catalog_names(kind,raw_name) VALUES(kind,NEW.raw_name)
     ON CONFLICT(kind,raw_name) DO UPDATE SET raw_name=EXCLUDED.raw_name RETURNING name_id INTO name_key;
 END IF;
 values_json=(to_jsonb(NEW)-ARRAY['raw_name','canonical_id'])||jsonb_build_object('name_id',name_key,'entity_id',NEW.canonical_id);
 SELECT string_agg(format('%I=v.%I',key,key),',') INTO assignments FROM jsonb_object_keys(values_json) key WHERE key<>'alias_id';
 EXECUTE format('UPDATE catalog_alias_reviews r SET %s FROM jsonb_populate_record(NULL::catalog_alias_reviews,$1) v WHERE r.alias_id=v.alias_id',assignments) USING values_json;
 GET DIAGNOSTICS affected=ROW_COUNT;
 IF affected=0 THEN
   INSERT INTO catalog_alias_reviews SELECT (jsonb_populate_record(NULL::catalog_alias_reviews,values_json)).*;
 END IF;
 PERFORM setval(pg_get_serial_sequence('catalog_alias_reviews','alias_id'),greatest(NEW.alias_id,(SELECT coalesce(max(alias_id),1) FROM catalog_alias_reviews)),true);
 RETURN NEW;
END $_$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_classification_command"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $$
#variable_conflict use_column
<<catalog_task_classification_command>>
DECLARE parent BIGINT=NULL; node BIGINT; label TEXT; translated TEXT; pos INTEGER; old_node BIGINT; actor TEXT;
BEGIN
 IF current_setting('manzara.catalog_projection',true)='on' THEN RETURN coalesce(NEW,OLD); END IF;
 IF TG_OP='DELETE' THEN
   UPDATE catalog_publication_metadata SET classification_id=NULL,revision=revision+1 WHERE classification_id=OLD.id; DELETE FROM catalog_classifications WHERE classification_id=OLD.id;RETURN OLD;
 END IF;
 IF jsonb_typeof(NEW.path_en::jsonb)<>'array' OR jsonb_array_length(NEW.path_en::jsonb)=0 THEN RAISE EXCEPTION 'empty classification path'; END IF;
 PERFORM pg_advisory_xact_lock(hashtext('catalog-taxonomy:__CATALOG_SCHEMA__'));
 FOR label,pos IN SELECT value,(ordinality-1)::integer FROM jsonb_array_elements_text(NEW.path_en::jsonb) WITH ORDINALITY LOOP
   IF btrim(label)='' THEN RAISE EXCEPTION 'empty classification label'; END IF;
   translated=NEW.path_tt::jsonb->>pos;
   SELECT node_id INTO node FROM catalog_classification_nodes WHERE ddc=NEW.ddc AND parent_id IS NOT DISTINCT FROM parent AND label_en=label FOR UPDATE;
   IF node IS NULL THEN
     INSERT INTO catalog_classification_nodes(ddc,parent_id,label_en,label_tt) VALUES(NEW.ddc,parent,label,translated) RETURNING node_id INTO node;
   ELSIF translated IS NOT NULL THEN
     IF EXISTS(SELECT 1 FROM catalog_protections WHERE record_kind='classification_node' AND record_key=node::text AND field='label_tt') THEN
       RAISE EXCEPTION 'protected classification translation; review in catalog';
     END IF;
     UPDATE catalog_classification_nodes SET label_tt=translated,revision=revision+1 WHERE node_id=node AND label_tt IS DISTINCT FROM translated;
   END IF;
   parent=node;
 END LOOP;
 INSERT INTO catalog_classifications(classification_id,node_id,status,created_by,created_at) VALUES(NEW.id,node,NEW.status,NEW.created_by,NEW.created_at)
 ON CONFLICT(classification_id) DO UPDATE SET node_id=EXCLUDED.node_id,status=EXCLUDED.status,revision=catalog_classifications.revision+1;
 IF TG_OP='UPDATE' AND (NEW.path_en::jsonb IS DISTINCT FROM OLD.path_en::jsonb OR NEW.path_tt::jsonb IS DISTINCT FROM OLD.path_tt::jsonb) THEN
   UPDATE catalog_publication_metadata SET revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE classification_id=NEW.id;
 END IF;
 PERFORM setval(pg_get_serial_sequence('catalog_classifications','classification_id'),greatest(NEW.id,(SELECT coalesce(max(classification_id),1) FROM catalog_classifications)),true);
 RETURN NEW;
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_collection_command"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $$
#variable_conflict use_column
<<catalog_task_collection_command>>
DECLARE c catalog_collections;
BEGIN
 IF current_setting('manzara.catalog_projection',true)='on' THEN RETURN coalesce(NEW,OLD); END IF;
 IF TG_OP='DELETE' THEN DELETE FROM catalog_collections WHERE collection_id=OLD.collection_id;RETURN OLD; END IF;
 INSERT INTO catalog_collections(collection_id,title,notes,include_in_library) VALUES(NEW.collection_id,NEW.title,NEW.notes,NEW.include_in_library=1)
 ON CONFLICT(collection_id) DO NOTHING;
 PERFORM catalog_task_patch('collection',NEW.collection_id::text,jsonb_build_object('title',NEW.title,'notes',NEW.notes,'include_in_library',NEW.include_in_library=1));
 SELECT * INTO c FROM catalog_collections WHERE collection_id=NEW.collection_id;
 NEW.normalized_title=CASE WHEN NEW.title=c.title THEN coalesce(NEW.normalized_title,"__CATALOG_SCHEMA__".catalog_title_key(c.title)) ELSE coalesce(c.normalized_title,"__CATALOG_SCHEMA__".catalog_title_key(c.title)) END;NEW.title=c.title;NEW.notes=c.notes;NEW.include_in_library=c.include_in_library::integer;
 UPDATE catalog_collections SET normalized_title=NEW.normalized_title,metadata_template_json=NEW.metadata_template_json,
 applied_at=NEW.applied_at,created_at=coalesce(created_at,NEW.created_at),source_updated_at=NEW.updated_at WHERE collection_id=NEW.collection_id;
 PERFORM setval(pg_get_serial_sequence('catalog_collections','collection_id'),greatest(NEW.collection_id,(SELECT coalesce(max(collection_id),1) FROM catalog_collections)),true);
 RETURN NEW;
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_document_command"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $_$
#variable_conflict use_column
<<catalog_task_document_command>>
DECLARE pub BIGINT; d catalog_document_metadata; y catalog_locations; s catalog_locations; t catalog_locations;
        fields JSONB; before JSONB; old_fields JSONB;
BEGIN
 IF current_setting('manzara.catalog_projection',true)='on' THEN RETURN coalesce(NEW,OLD); END IF;
 IF TG_OP='DELETE' THEN
   DELETE FROM catalog_document_metadata WHERE md5=OLD.md5; RETURN OLD;
 END IF;
 IF NEW.md5 !~ '^[0-9a-f]{32}$' THEN RAISE EXCEPTION 'invalid document md5'; END IF;
 SELECT * INTO d FROM catalog_document_metadata WHERE md5=NEW.md5 FOR UPDATE;
 IF d.md5 IS NULL THEN
   INSERT INTO catalog_publication_metadata(has_metadata,metadata_present) VALUES(false,false) RETURNING publication_id INTO pub;
   INSERT INTO catalog_document_metadata(md5,publication_id,mime_type,complete,restricted)
     VALUES(NEW.md5,pub,NEW.mime_type,coalesce(NEW."full",false),coalesce(NEW.sharing_restricted,true));
 ELSE pub=d.publication_id; END IF;
 PERFORM catalog_task_patch('document',NEW.md5,jsonb_build_object('mime_type',NEW.mime_type,'complete',coalesce(NEW."full",false),
   'restricted',coalesce(NEW.sharing_restricted,true),'content_extraction_method',NEW.content_extraction_method,
   'meta_extraction_method',NEW.meta_extraction_method),NEW.md5);
 IF NEW.language IS NOT NULL AND (TG_OP='INSERT' OR NEW.language IS DISTINCT FROM OLD.language) THEN
   PERFORM catalog_task_patch('publication',pub::text,jsonb_build_object('languages',to_jsonb(ARRAY(SELECT btrim(x)
     FROM unnest(string_to_array(NEW.language,',')) x WHERE btrim(x)<>''))),NEW.md5);
 END IF;
 SELECT * INTO d FROM catalog_document_metadata WHERE md5=NEW.md5;
 INSERT INTO catalog_locations(md5,provider,purpose,source_path,resource_id,public_url,public_key)
 VALUES(NEW.md5,'yandex','source',NEW.ya_path,NEW.ya_resource_id,CASE WHEN NOT d.restricted THEN NEW.ya_public_url END,
   CASE WHEN NOT d.restricted THEN NEW.ya_public_key END)
 ON CONFLICT(md5,provider,purpose) DO UPDATE SET source_path=EXCLUDED.source_path,resource_id=EXCLUDED.resource_id,
   public_url=EXCLUDED.public_url,public_key=EXCLUDED.public_key,revision=catalog_locations.revision+1;
 INSERT INTO catalog_locations(md5,provider,purpose,locator,size,etag,verified_at)
 VALUES(NEW.md5,'s3','primary',NEW.document_url,NEW.primary_storage_size,NEW.primary_storage_etag,NEW.primary_storage_verified_at)
 ON CONFLICT(md5,provider,purpose) DO UPDATE SET locator=EXCLUDED.locator,size=EXCLUDED.size,etag=EXCLUDED.etag,
   verified_at=EXCLUDED.verified_at,revision=catalog_locations.revision+1;
 INSERT INTO catalog_locations(md5,provider,purpose,locator) VALUES(NEW.md5,'s3','content',NEW.content_url)
 ON CONFLICT(md5,provider,purpose) DO UPDATE SET locator=EXCLUDED.locator,revision=catalog_locations.revision+1;
 NEW.mime_type=d.mime_type;NEW."full"=d.complete;NEW.sharing_restricted=d.restricted;
 NEW.content_extraction_method=d.content_extraction_method;NEW.meta_extraction_method=d.meta_extraction_method;
 SELECT nullif(array_to_string(languages,','),'') INTO NEW.language FROM catalog_publication_metadata WHERE publication_id=pub;
 IF d.restricted THEN NEW.ya_public_url=NULL;NEW.ya_public_key=NULL; END IF;
 RETURN NEW;
END $_$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_identity_command"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $$
#variable_conflict use_column
<<command>>
DECLARE e catalog_entities; fields JSONB; name_key BIGINT; p BIGINT; actor TEXT; old_display TEXT;
BEGIN
 IF current_setting('manzara.catalog_projection',true)='on' THEN RETURN coalesce(NEW,OLD); END IF;
 actor=coalesce(nullif(current_setting('manzara.catalog_actor',true),''),'task');
 IF TG_OP='DELETE' THEN
   DELETE FROM catalog_aliases WHERE entity_id=OLD.canonical_id;
   UPDATE catalog_entities SET status='archived',revision=revision+1 WHERE entity_id=OLD.canonical_id;
   RETURN OLD;
 END IF;
 fields=to_jsonb(NEW)-ARRAY['canonical_id','entity_type','normalized_name','created_at','updated_at'];
 INSERT INTO catalog_entities(entity_id,kind,display_name,status,approval)
 VALUES(NEW.canonical_id,CASE NEW.entity_type WHEN 'personality' THEN 'person' ELSE 'organization' END,NEW.display_name,'active',
   CASE WHEN actor='task' THEN 'unconfirmed' ELSE 'confirmed' END) ON CONFLICT(entity_id) DO NOTHING;
 SELECT * INTO e FROM catalog_entities WHERE entity_id=NEW.canonical_id FOR UPDATE;
 old_display=e.display_name;
 IF actor<>'task' THEN fields=fields||jsonb_build_object('approval','confirmed'); END IF;
 IF NEW.display_name IS DISTINCT FROM e.display_name THEN
   INSERT INTO catalog_names(kind,raw_name) VALUES(e.kind,e.display_name) ON CONFLICT(kind,raw_name) DO UPDATE SET raw_name=EXCLUDED.raw_name RETURNING name_id INTO name_key;
   INSERT INTO catalog_aliases(name_id,entity_id,approval) VALUES(name_key,e.entity_id,e.approval) ON CONFLICT(name_id,entity_id) DO NOTHING;
 END IF;
 PERFORM catalog_task_patch('entity',e.entity_id::text,fields);
 INSERT INTO catalog_entity_roles(entity_id,role) VALUES(e.entity_id,CASE NEW.entity_type WHEN 'publisher' THEN 'publisher' ELSE 'personality' END) ON CONFLICT DO NOTHING;
 SELECT * INTO e FROM catalog_entities WHERE entity_id=NEW.canonical_id;
 IF e.display_name IS DISTINCT FROM old_display THEN
   UPDATE catalog_publication_metadata SET revision=revision+1,updated_at=CURRENT_TIMESTAMP
     WHERE publication_id IN (SELECT publication_id FROM catalog_contribution_metadata WHERE entity_id=e.entity_id);
 END IF;
 IF e.status='merged' AND e.merged_into_id IS NOT NULL THEN
   IF e.merged_into_id=e.entity_id OR NOT EXISTS(SELECT 1 FROM catalog_entities WHERE entity_id=e.merged_into_id AND status='active' AND kind=e.kind) THEN
     RAISE EXCEPTION 'invalid identity merge target'; END IF;
   INSERT INTO catalog_aliases(name_id,entity_id,approval) SELECT name_id,e.merged_into_id,approval FROM catalog_aliases WHERE entity_id=e.entity_id ON CONFLICT(name_id,entity_id) DO NOTHING;
   INSERT INTO catalog_entity_roles(entity_id,role) SELECT e.merged_into_id,role FROM catalog_entity_roles WHERE entity_id=e.entity_id ON CONFLICT DO NOTHING;
   -- Migrate reviewed identities, never every occurrence of a spelling.
   FOR p IN SELECT DISTINCT publication_id FROM catalog_contribution_metadata WHERE entity_id=e.entity_id LOOP
     UPDATE catalog_publication_metadata SET revision=revision+1 WHERE publication_id=p;
   END LOOP;
   UPDATE catalog_contribution_metadata SET entity_id=e.merged_into_id,revision=revision+1 WHERE entity_id=e.entity_id;
 END IF;
 NEW.display_name=e.display_name;NEW.normalized_name="__CATALOG_SCHEMA__".catalog_name_key(e.display_name);NEW.status=e.status;NEW.merged_into_id=e.merged_into_id;
 NEW.notes=e.notes;NEW.surname_full=e.surname_full;NEW.surname_initials=e.surname_initials;NEW.name_full=e.name_full;
 NEW.name_initials=e.name_initials;NEW.father_name_full=e.father_name_full;NEW.father_name_initials=e.father_name_initials;
 NEW.title=e.title;NEW.sex=e.sex;NEW.identity_key=e.identity_key;
 UPDATE catalog_entities SET normalized_name=NEW.normalized_name,created_at=coalesce(created_at,NEW.created_at),source_updated_at=NEW.updated_at WHERE entity_id=NEW.canonical_id;
 PERFORM setval(pg_get_serial_sequence('catalog_entities','entity_id'),greatest(NEW.canonical_id,(SELECT coalesce(max(entity_id),1) FROM catalog_entities)),true);
 RETURN NEW;
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_membership_command"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $$
#variable_conflict use_column
<<catalog_task_membership_command>>
DECLARE pub BIGINT; membership BIGINT;
BEGIN
 IF current_setting('manzara.catalog_projection',true)='on' THEN RETURN coalesce(NEW,OLD); END IF;
 SELECT publication_id INTO pub FROM catalog_document_metadata WHERE md5=CASE WHEN TG_OP='DELETE' THEN OLD.md5 ELSE NEW.md5 END;
 IF pub IS NULL THEN RETURN coalesce(NEW,OLD); END IF;
 IF TG_OP='INSERT' AND EXISTS(SELECT 1 FROM library_collection_items WHERE md5=NEW.md5) THEN RETURN NEW; END IF;
 PERFORM catalog_task_patch('publication',pub::text,jsonb_build_object('collection_id',CASE WHEN TG_OP='DELETE' THEN NULL ELSE NEW.collection_id END),CASE WHEN TG_OP='DELETE' THEN OLD.md5 ELSE NEW.md5 END);
 UPDATE catalog_publication_metadata SET collection_item_title=CASE WHEN TG_OP='DELETE' THEN NULL ELSE NEW.item_title END,
 collection_created_at=CASE WHEN TG_OP='DELETE' THEN NULL ELSE NEW.created_at END,
 collection_updated_at=CASE WHEN TG_OP='DELETE' THEN NULL ELSE NEW.updated_at END WHERE publication_id=pub;
 IF TG_OP<>'DELETE' THEN
   SELECT collection_id INTO membership FROM catalog_publication_metadata WHERE publication_id=pub;
   IF membership IS NULL THEN RAISE EXCEPTION 'collection assignment awaits review'; END IF;
   NEW.collection_id=membership;
 END IF;
 RETURN coalesce(NEW,OLD);
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_metadata"("digest" "text", "source" "jsonb") RETURNS "void"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $$
#variable_conflict use_column
<<catalog_task_metadata>>
DECLARE pub BIGINT; old JSONB; normalized JSONB; scalar JSONB='{}'; field TEXT; column_name TEXT;
        value JSONB; protected TEXT[]; doc_protected TEXT[]; blocked JSONB='{}';
        actor TEXT; role TEXT; item JSONB; person JSONB; pos INTEGER; nested INTEGER;
        raw TEXT; kind TEXT; name_key BIGINT; current_credit catalog_contribution_metadata; protected_credits BOOLEAN;
BEGIN
 IF source IS NULL THEN RETURN; END IF;
 IF jsonb_typeof(source)<>'object' OR source->>'@context' NOT IN ('https://schema.org') THEN
   RAISE EXCEPTION 'unsupported metadata envelope'; END IF;
 IF EXISTS(SELECT 1 FROM jsonb_object_keys(source) f WHERE f NOT IN
   ('@context','@type','name','description','datePublished','numberOfPages','bookEdition','inLanguage',
    'author','editor','translator','illustrator','publisher','contributor','isbn','genre','about','audience','accessMode','accessModeSufficient','isBasedOn')) THEN
   RAISE EXCEPTION 'unsupported metadata fields'; END IF;
 SELECT publication_id INTO pub FROM catalog_document_metadata WHERE md5=digest FOR UPDATE;
 PERFORM 1 FROM catalog_publication_metadata WHERE publication_id=pub FOR UPDATE;
 actor=coalesce(nullif(current_setting('manzara.catalog_actor',true),''),'task');
 old=catalog_schema_org(digest);
 SELECT coalesce(array_agg(field),'{}') INTO protected FROM catalog_protections WHERE record_kind='publication' AND record_key=pub::text AND catalog_task_metadata.actor='task';
 SELECT coalesce(array_agg(field),'{}') INTO doc_protected FROM catalog_protections WHERE record_kind='document' AND record_key=digest AND catalog_task_metadata.actor='task';
 FOR field,column_name IN SELECT * FROM (VALUES ('@type','work_type'),('name','name'),('description','description'),
   ('bookEdition','edition'),('datePublished','date_published'),('numberOfPages','page_count')) x LOOP
   IF source ? field THEN scalar=scalar||jsonb_build_object(column_name,source->field); END IF;
 END LOOP;
 scalar=scalar||jsonb_build_object('languages',to_jsonb(ARRAY(SELECT btrim(x) FROM unnest(string_to_array(coalesce(source->>'inLanguage',''),',')) x WHERE btrim(x)<>'')));
 IF source->'audience' IS DISTINCT FROM old->'audience' THEN
   IF 'audiences'=ANY(protected) THEN blocked=blocked||jsonb_build_object('audiences',catalog_array(source->'audience'),'audience_array',jsonb_typeof(source->'audience')='array');
   ELSE scalar=scalar||jsonb_build_object('audience_array',coalesce(jsonb_typeof(source->'audience')='array',false));
     DELETE FROM catalog_audiences WHERE publication_id=pub;
     INSERT INTO catalog_audiences(publication_id,position,kind,audience_type,min_age,max_age)
       SELECT pub,(ordinality-1)::integer,value->>'@type',value->>'audienceType',(value->>'suggestedMinAge')::integer,(value->>'suggestedMaxAge')::integer
       FROM jsonb_array_elements(catalog_array(source->'audience')) WITH ORDINALITY;
   END IF;
 END IF;
 PERFORM catalog_task_patch('publication',pub::text,scalar,digest);
 -- Retain reviewed mentions when a canonical projection is written back.
 protected_credits='credits'=ANY(protected);
 normalized='[]';
 FOREACH role IN ARRAY ARRAY['author','editor','translator','illustrator','publisher','contributor'] LOOP
   FOR item,pos IN SELECT value,(ordinality-1)::integer FROM jsonb_array_elements(catalog_array(source->role)) WITH ORDINALITY LOOP
     FOR person,nested IN SELECT value,(ordinality-1)::integer FROM jsonb_array_elements(catalog_array(CASE WHEN item->>'@type'='Role' THEN item->'contributor' ELSE item END)) WITH ORDINALITY LOOP
       raw=person->>'name'; kind=lower(person->>'@type');
       IF kind NOT IN ('person','organization') OR raw IS NULL OR EXISTS(SELECT 1 FROM jsonb_object_keys(person) f WHERE f NOT IN ('@type','name')) THEN RAISE EXCEPTION 'unsupported credited entity'; END IF;
       SELECT c.* INTO current_credit FROM catalog_contribution_metadata c JOIN catalog_names n USING(name_id) LEFT JOIN catalog_entities e USING(entity_id)
         WHERE c.publication_id=pub AND c.role=catalog_task_metadata.role AND c.position=pos AND c.nested_position=nested AND n.kind=catalog_task_metadata.kind
         AND c.role_name IS NOT DISTINCT FROM (CASE WHEN item->>'@type'='Role' THEN item->>'roleName' END)
         AND (n.raw_name=raw OR e.display_name=raw);
       IF current_credit.contribution_id IS NOT NULL THEN SELECT raw_name INTO raw FROM catalog_names WHERE name_id=current_credit.name_id; END IF;
       normalized=normalized||jsonb_build_array(jsonb_build_object('role',role,'role_name',CASE WHEN item->>'@type'='Role' THEN item->>'roleName' END,
         'position',pos,'nested_position',nested,'raw_name',raw,'kind',kind));
       IF NOT protected_credits AND current_credit.contribution_id IS NULL THEN
         INSERT INTO catalog_names(kind,raw_name) VALUES(kind,raw) ON CONFLICT(kind,raw_name) DO UPDATE SET raw_name=EXCLUDED.raw_name RETURNING name_id INTO name_key;
         INSERT INTO catalog_contribution_metadata(publication_id,name_id,role,role_name,position,nested_position)
           VALUES(pub,name_key,role,CASE WHEN item->>'@type'='Role' THEN item->>'roleName' END,pos,nested);
       END IF;
     END LOOP;
   END LOOP;
 END LOOP;
 SELECT coalesce(jsonb_agg(x ORDER BY x->>'role',(x->>'position')::integer,(x->>'nested_position')::integer),'[]') INTO normalized FROM jsonb_array_elements(normalized) x;
 IF protected_credits THEN
   IF normalized IS DISTINCT FROM (SELECT coalesce(jsonb_agg(jsonb_build_object('role',c.role,'role_name',c.role_name,'position',c.position,
      'nested_position',c.nested_position,'raw_name',n.raw_name,'kind',n.kind) ORDER BY c.role,c.position,c.nested_position),'[]')
      FROM catalog_contribution_metadata c JOIN catalog_names n USING(name_id) WHERE publication_id=pub) THEN blocked=blocked||jsonb_build_object('credits',normalized); END IF;
 ELSE
   DELETE FROM catalog_contribution_metadata c WHERE publication_id=pub AND NOT EXISTS(
     SELECT 1 FROM jsonb_array_elements(normalized) x JOIN catalog_names n ON n.kind=x->>'kind' AND n.raw_name=x->>'raw_name'
     WHERE c.name_id=n.name_id AND c.role=x->>'role' AND c.position=(x->>'position')::integer AND c.nested_position=(x->>'nested_position')::integer
       AND c.role_name IS NOT DISTINCT FROM x->>'role_name');
 END IF;
 FOR field,column_name IN SELECT * FROM (VALUES ('isbn','identifiers'),('genre','genres'),('about','subjects')) x LOOP
   value=catalog_array(source->field);
   IF value=catalog_array(old->field) THEN CONTINUE; END IF;
   IF column_name=ANY(protected) THEN blocked=blocked||jsonb_build_object(column_name,value);CONTINUE; END IF;
   IF field='isbn' THEN
     DELETE FROM catalog_identifiers WHERE publication_id=pub;
     INSERT INTO catalog_identifiers(publication_id,kind,value,normalized,position)
       SELECT pub,'isbn',v,regexp_replace(upper(v),'[^0-9X]','','g'),(ordinality-1)::integer FROM jsonb_array_elements_text(value) WITH ORDINALITY x(v,ordinality);
   ELSIF field='genre' THEN
     DELETE FROM catalog_genres WHERE publication_id=pub;
     INSERT INTO catalog_genres(publication_id,value,position) SELECT pub,v,(ordinality-1)::integer FROM jsonb_array_elements_text(value) WITH ORDINALITY x(v,ordinality);
   ELSE
     DELETE FROM catalog_subjects WHERE publication_id=pub;
     INSERT INTO catalog_subjects(publication_id,name,term_code,set_name,set_url,set_is_url,position)
       SELECT pub,v->>'name',v->>'termCode',v->'inDefinedTermSet'->>'name',
         CASE WHEN jsonb_typeof(v->'inDefinedTermSet')='string' THEN v->>'inDefinedTermSet' ELSE v->'inDefinedTermSet'->>'url' END,
         jsonb_typeof(v->'inDefinedTermSet')='string',(ordinality-1)::integer FROM jsonb_array_elements(value) WITH ORDINALITY x(v,ordinality)
       WHERE NOT EXISTS(SELECT 1 FROM catalog_publication_metadata WHERE publication_id=pub AND classification_id IS NOT NULL)
         OR lower(coalesce(v->'inDefinedTermSet'->>'name','')) NOT IN ('ddc','categorypath');
   END IF;
 END LOOP;
 IF catalog_array(source->'accessMode') IS DISTINCT FROM catalog_array(old->'accessMode') THEN
   IF 'access_modes'=ANY(doc_protected) THEN blocked=blocked||jsonb_build_object('access_modes',catalog_array(source->'accessMode'));
   ELSE PERFORM catalog_task_patch('document',digest,jsonb_build_object('access_modes',catalog_array(source->'accessMode')),digest); END IF;
 END IF;
 IF catalog_array(source->'accessModeSufficient') IS DISTINCT FROM catalog_array(old->'accessModeSufficient') THEN
   IF 'sufficient_modes'=ANY(doc_protected) THEN blocked=blocked||jsonb_build_object('sufficient_modes',(SELECT coalesce(jsonb_agg(v->'itemListElement'),'[]') FROM jsonb_array_elements(catalog_array(source->'accessModeSufficient')) v));
   ELSE DELETE FROM catalog_sufficient_mode_metadata WHERE md5=digest;
     INSERT INTO catalog_sufficient_mode_metadata(md5,position,modes) SELECT digest,(ordinality-1)::integer,
       ARRAY(SELECT jsonb_array_elements_text(v->'itemListElement')) FROM jsonb_array_elements(catalog_array(source->'accessModeSufficient')) WITH ORDINALITY x(v,ordinality);
   END IF;
 END IF;
 IF source->'isBasedOn' IS DISTINCT FROM old->'isBasedOn' THEN
   value=source->'isBasedOn';
   IF 'based_on'=ANY(protected) THEN blocked=blocked||jsonb_build_object('based_on',value);
   ELSE DELETE FROM catalog_reference_authors WHERE publication_id=pub; DELETE FROM catalog_reference_metadata WHERE publication_id=pub;
     IF value IS NOT NULL AND value<>'null' THEN
       IF EXISTS(SELECT 1 FROM jsonb_object_keys(value) f WHERE f NOT IN ('@type','name','inLanguage','url','author')) THEN RAISE EXCEPTION 'unsupported source-work reference'; END IF;
       INSERT INTO catalog_reference_metadata(publication_id,work_type,name,language,urls) VALUES(pub,value->>'@type',value->>'name',value->>'inLanguage',CASE WHEN value ? 'url' THEN ARRAY(SELECT jsonb_array_elements_text(catalog_array(value->'url'))) END);
       INSERT INTO catalog_reference_authors(publication_id,position,kind,name) SELECT pub,(ordinality-1)::integer,v->>'@type',v->>'name'
         FROM jsonb_array_elements(catalog_array(value->'author')) WITH ORDINALITY x(v,ordinality);
     END IF;
   END IF;
 END IF;
 IF blocked<>'{}' THEN INSERT INTO catalog_proposals(kind,publication_id,evidence,field_changes)
   VALUES('metadata',pub,jsonb_build_object('md5',digest,'actor',actor),blocked); END IF;
 INSERT INTO catalog_evidence(md5,record_kind,record_key,source,payload) VALUES(digest,'publication',pub::text,'task.metadata',source);
 -- Relations affect the same optimistic revision as columns.
 UPDATE catalog_publication_metadata SET revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE publication_id=pub;
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_metadata_command"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $$
#variable_conflict use_column
<<catalog_task_metadata_command>>
DECLARE pub catalog_publication_metadata; changes JSONB='{}';
BEGIN
 IF current_setting('manzara.catalog_projection',true)='on' THEN RETURN coalesce(NEW,OLD); END IF;
 IF TG_OP='DELETE' THEN
   IF NOT EXISTS(SELECT 1 FROM catalog_document_metadata WHERE md5=OLD.md5) THEN RETURN OLD; END IF;
   RAISE EXCEPTION 'metadata deletion requires explicit catalog review';
 END IF;
 SELECT p.* INTO pub FROM catalog_publication_metadata p JOIN catalog_document_metadata d USING(publication_id) WHERE d.md5=NEW.md5 FOR UPDATE OF p;
 IF pub.publication_id IS NULL THEN RAISE EXCEPTION 'document missing'; END IF;
 UPDATE catalog_publication_metadata SET has_metadata=true,metadata_present=NEW.schema_org IS NOT NULL WHERE publication_id=pub.publication_id;
 IF TG_OP='INSERT' OR NEW.schema_org::jsonb IS DISTINCT FROM OLD.schema_org::jsonb THEN
   PERFORM catalog_task_metadata(NEW.md5,NEW.schema_org::jsonb);
 END IF;
 IF TG_OP='INSERT' OR NEW.lib IS DISTINCT FROM OLD.lib THEN changes=changes||jsonb_build_object('inclusion',CASE NEW.lib WHEN TRUE THEN 'included' WHEN FALSE THEN 'excluded' ELSE 'pending' END); END IF;
 IF TG_OP='INSERT' OR NEW.lib_eval_method IS DISTINCT FROM OLD.lib_eval_method THEN changes=changes||jsonb_build_object('evaluation_method',NEW.lib_eval_method); END IF;
 IF TG_OP='INSERT' OR NEW.classification_id IS DISTINCT FROM OLD.classification_id THEN changes=changes||jsonb_build_object('classification_id',NEW.classification_id); END IF;
 PERFORM catalog_task_patch('publication',pub.publication_id::text,changes,NEW.md5);
 SELECT * INTO pub FROM catalog_publication_metadata WHERE publication_id=pub.publication_id;
 NEW.schema_org=catalog_schema_org(NEW.md5);NEW.lib=CASE pub.inclusion WHEN 'included' THEN TRUE WHEN 'excluded' THEN FALSE END;
 NEW.lib_eval_method=pub.evaluation_method;NEW.classification_id=pub.classification_id;
 RETURN NEW;
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_patch"("kind" "text", "key" "text", "changes" "jsonb", "digest" "text" DEFAULT NULL::"text") RETURNS "void"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $_$
#variable_conflict use_column
<<catalog_task_patch>>
DECLARE relation TEXT; pk TEXT; before JSONB; after JSONB; allowed JSONB='{}'; blocked JSONB='{}';
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
 EXECUTE format('UPDATE %I r SET %s,revision=r.revision+1,updated_at=CURRENT_TIMESTAMP FROM jsonb_populate_record(NULL::%I,$2) v WHERE r.%I::text=$1 RETURNING to_jsonb(r)',relation,assignments,relation,pk)
 INTO after USING key,allowed;
 INSERT INTO catalog_revisions(record_kind,record_key,actor,"before","after") VALUES(kind,key,actor,before,after);
 IF actor<>'task' THEN
   INSERT INTO catalog_protections(record_kind,record_key,field,actor) SELECT kind,key,f,actor FROM jsonb_object_keys(allowed) f
     ON CONFLICT(record_kind,record_key,field) DO UPDATE SET actor=EXCLUDED.actor;
 END IF;
END $_$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_review_command"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public', 'public'
    AS $$
#variable_conflict use_column
<<catalog_task_review_command>>
DECLARE source TEXT; legacy_id BIGINT; proposal BIGINT; data JSONB; display TEXT; members TEXT[];
        key TEXT; kind TEXT; status TEXT; existing_status TEXT;
BEGIN
 IF current_setting('manzara.catalog_projection',true)='on' THEN RETURN coalesce(NEW,OLD); END IF;
 data=CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
 source='legacy.'||TG_TABLE_NAME;
 legacy_id=(data->>CASE TG_TABLE_NAME WHEN 'normalization_suggestions' THEN 'suggestion_id' ELSE 'proposal_id' END)::bigint;
 SELECT proposal_id,c.status INTO proposal,existing_status FROM catalog_proposals c WHERE evidence->>'source'=source AND (evidence->>'legacy_id')::bigint=legacy_id FOR UPDATE;
 IF TG_OP='DELETE' THEN
   UPDATE catalog_proposals SET status='rejected',revision=revision+1 WHERE proposal_id=proposal AND catalog_proposals.status IN ('pending','deferred');RETURN OLD;
 END IF;
 status=CASE data->>'status' WHEN 'open' THEN 'pending' WHEN 'pending' THEN 'pending' WHEN 'staged' THEN 'pending'
   WHEN 'skipped' THEN 'deferred' WHEN 'separate' THEN 'separate' WHEN 'applied' THEN 'applied' WHEN 'accepted' THEN 'applied' ELSE 'rejected' END;
 IF status NOT IN ('pending','deferred') THEN
   UPDATE catalog_proposals SET status=catalog_task_review_command.status,revision=revision+1 WHERE proposal_id=proposal AND catalog_proposals.status IN ('pending','deferred');RETURN NEW;
 END IF;
 IF existing_status IS NOT NULL AND existing_status NOT IN ('pending','deferred') THEN RETURN NEW; END IF;
 IF TG_TABLE_NAME='normalization_suggestions' THEN
   kind=CASE NEW.entity_type WHEN 'personality' THEN 'person' ELSE 'organization' END;
   display=NEW.normalized_name;members=ARRAY['raw:'||NEW.raw_name];
   IF NEW.target_canonical_id IS NOT NULL THEN members=array_append(members,'canonical:'||NEW.target_canonical_id); END IF;
 ELSE
   kind='organization';display=coalesce(NEW.review_edit->>'display_name',NEW.proposal->>'proposed_name');
   members=ARRAY(SELECT jsonb_array_elements_text(coalesce(NEW.review_edit->'member_ids',NEW.proposal->'member_ids')));
 END IF;
 IF cardinality(members)=0 OR cardinality(members)>200 THEN RAISE EXCEPTION 'review cluster requires bounded members'; END IF;
 IF proposal IS NULL THEN
   INSERT INTO catalog_proposals(kind,status,display_name,evidence) VALUES('identity',status,display,jsonb_build_object('source',source,'legacy_id',legacy_id,'legacy_status',data->>'status','original',data)) RETURNING proposal_id INTO proposal;
 ELSE
   UPDATE catalog_proposals SET display_name=display,status=catalog_task_review_command.status,evidence=jsonb_build_object('source',source,'legacy_id',legacy_id,'legacy_status',data->>'status','original',data),revision=revision+1 WHERE proposal_id=proposal;
   DELETE FROM catalog_proposal_members WHERE proposal_id=proposal;
 END IF;
 FOREACH key IN ARRAY members LOOP PERFORM catalog_review_member(proposal,key,kind); END LOOP;
 RETURN NEW;
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_title_key"("value" "text") RETURNS "text"
    LANGUAGE "sql" IMMUTABLE STRICT PARALLEL SAFE
    AS $$
 SELECT btrim(regexp_replace("__CATALOG_SCHEMA__".catalog_name_key(value),
   '[^0-9a-zа-яёәҗңөүһіғқҫ]+',' ','g'))
$$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_upsert"("target" "text", "payload" "jsonb", "conflict_fields" "text"[], "update_fields" "text"[] DEFAULT NULL::"text"[], "prefer_existing" "text"[] DEFAULT '{}'::"text"[]) RETURNS "jsonb"
    LANGUAGE "plpgsql"
    SET "search_path" TO '__CATALOG_SCHEMA__', 'public'
    AS $_$
#variable_conflict use_column
DECLARE relation REGCLASS; predicate TEXT; columns TEXT; expressions TEXT; assignments TEXT;
        previous JSONB; result JSONB; conflict_value JSONB; count_rows BIGINT; key TEXT;
BEGIN
 IF jsonb_typeof(payload)<>'object' OR payload='{}' THEN RAISE EXCEPTION 'upsert requires an object'; END IF;
 IF NOT (CASE target
   WHEN 'document' THEN conflict_fields=ARRAY['md5']
   WHEN 'metadata' THEN conflict_fields=ARRAY['md5']
   WHEN 'classification' THEN conflict_fields IN (ARRAY['id'],ARRAY['ddc','path_en_key'])
   WHEN 'normalization_canonicals' THEN conflict_fields IN (ARRAY['canonical_id'],ARRAY['entity_type','normalized_name'])
   WHEN 'normalization_aliases' THEN conflict_fields IN (ARRAY['alias_id'],ARRAY['entity_type','raw_name'])
   WHEN 'library_collections' THEN conflict_fields IN (ARRAY['collection_id'],ARRAY['normalized_title'])
   WHEN 'library_collection_items' THEN conflict_fields IN (ARRAY['md5'],ARRAY['collection_id','md5'])
   ELSE FALSE END) THEN RAISE EXCEPTION 'unsupported catalog conflict key'; END IF;
 relation=to_regclass(target);
 IF relation IS NULL THEN RAISE EXCEPTION 'catalog command target missing'; END IF;
 FOR key IN SELECT jsonb_object_keys(payload) UNION SELECT unnest(update_fields) UNION SELECT unnest(prefer_existing) LOOP
   IF NOT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=relation AND attname=key AND attnum>0 AND NOT attisdropped) THEN
     RAISE EXCEPTION 'unknown catalog command column %',key;
   END IF;
 END LOOP;
 IF EXISTS(SELECT 1 FROM unnest(conflict_fields) f WHERE NOT payload ? f OR payload->f='null') THEN
   RAISE EXCEPTION 'catalog conflict key required'; END IF;
 SELECT jsonb_object_agg(f,payload->f) INTO conflict_value FROM unnest(conflict_fields) f;
 PERFORM pg_advisory_xact_lock(hashtextextended('catalog-upsert:'||target||':'||conflict_value::text,0));
 SELECT string_agg(format('r.%I IS NOT DISTINCT FROM v.%I',f,f),' AND ') INTO predicate FROM unnest(conflict_fields) f;
 EXECUTE format('SELECT count(*),jsonb_agg(to_jsonb(r))->0 FROM %s r CROSS JOIN jsonb_populate_record(NULL::%s,$1) v WHERE %s',relation,relation,predicate)
 INTO count_rows,previous USING payload;
 IF count_rows>1 THEN RAISE EXCEPTION 'duplicate catalog conflict key'; END IF;
 IF count_rows=1 THEN
   IF update_fields IS NULL OR cardinality(update_fields)=0 THEN RETURN previous; END IF;
   IF EXISTS(SELECT 1 FROM unnest(update_fields) f WHERE NOT payload ? f) THEN RAISE EXCEPTION 'catalog update value missing'; END IF;
   SELECT string_agg(CASE WHEN f=ANY(prefer_existing) THEN format('%I=coalesce(r.%I,v.%I)',f,f,f)
     ELSE format('%I=v.%I',f,f) END,',') INTO assignments FROM unnest(update_fields) f;
   EXECUTE format('UPDATE %s r SET %s FROM jsonb_populate_record(NULL::%s,$1) v WHERE %s RETURNING to_jsonb(r)',relation,assignments,relation,predicate)
   INTO result USING payload;
 ELSE
   SELECT string_agg(format('%I',f),','),string_agg(format('v.%I',f),',') INTO columns,expressions FROM jsonb_object_keys(payload) f;
   EXECUTE format('INSERT INTO %s (%s) SELECT %s FROM jsonb_populate_record(NULL::%s,$1) v RETURNING to_jsonb(%I.*)',relation,columns,expressions,relation,target)
   INTO result USING payload;
 END IF;
 RETURN result;
END $_$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_validate_reference_urls"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
DECLARE present BOOLEAN;
BEGIN
 IF TG_TABLE_NAME='catalog_reference_urls' THEN
   SELECT urls_present INTO present FROM "__CATALOG_SCHEMA__".catalog_references
     WHERE publication_id=NEW.publication_id FOR UPDATE;
   IF present IS FALSE THEN RAISE EXCEPTION 'reference URL list must be present before inserting URLs'; END IF;
 ELSIF NOT NEW.urls_present AND EXISTS(SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_reference_urls
   WHERE publication_id=NEW.publication_id) THEN
   RAISE EXCEPTION 'delete reference URLs before marking their list absent';
 END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION "__CATALOG_SCHEMA__"."catalog_validate_topology"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $_$
DECLARE identity BIGINT; target BIGINT; visited BIGINT[]; relation TEXT; edge TEXT;
BEGIN
 identity=(to_jsonb(NEW)->>TG_ARGV[0])::bigint;
 edge=TG_ARGV[1];target=(to_jsonb(NEW)->>edge)::bigint;visited=ARRAY[identity];
 relation=format('%I.%I',TG_TABLE_SCHEMA,TG_TABLE_NAME);
 IF TG_TABLE_NAME='catalog_classification_nodes' THEN
   IF EXISTS(SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_classification_nodes
     WHERE node_id=target AND ddc<>NEW.ddc) OR EXISTS(
     SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_classification_nodes WHERE parent_id=identity AND ddc<>NEW.ddc) THEN
     RAISE EXCEPTION 'classification parent and children must use the same DDC';
   END IF;
 END IF;
 WHILE target IS NOT NULL LOOP
   IF target=ANY(visited) THEN RAISE EXCEPTION 'catalog % contains a cycle',TG_TABLE_NAME; END IF;
   visited=array_append(visited,target);
   EXECUTE format('SELECT %I FROM %s WHERE %I=$1',edge,relation,TG_ARGV[0]) INTO target USING target;
 END LOOP;
 RETURN NEW;
END $_$;

SET LOCAL default_tablespace = '';

SET LOCAL default_table_access_method = "heap";

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_alias_reviews" (
    "alias_id" bigint NOT NULL,
    "name_id" bigint NOT NULL,
    "entity_type" "text" NOT NULL,
    "entity_id" bigint,
    "normalized_name" "text" NOT NULL,
    "script_label" "text" NOT NULL,
    "decision_status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "docs_count" bigint DEFAULT '0'::bigint NOT NULL,
    "mentions_count" bigint DEFAULT '0'::bigint NOT NULL,
    "marker_count" bigint DEFAULT '0'::bigint NOT NULL,
    "confidence" double precision,
    "source" "text",
    "reason" "text",
    "created_at" "text",
    "updated_at" "text",
    "successful_model" "text",
    "prompt_version" "text",
    "schema_version" "text",
    "surname_full" "text",
    "surname_initials" "text",
    "name_full" "text",
    "name_initials" "text",
    "father_name_full" "text",
    "father_name_initials" "text",
    "title" "text",
    "sex" "text",
    "source_roles" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL
);

CREATE SEQUENCE "__CATALOG_SCHEMA__"."catalog_alias_reviews_alias_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "__CATALOG_SCHEMA__"."catalog_alias_reviews_alias_id_seq" OWNED BY "__CATALOG_SCHEMA__"."catalog_alias_reviews"."alias_id";

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_aliases" (
    "alias_id" bigint NOT NULL,
    "name_id" bigint NOT NULL,
    "entity_id" bigint NOT NULL,
    "approval" "text" DEFAULT 'unconfirmed'::"text" NOT NULL,
    "revision" bigint DEFAULT '1'::bigint NOT NULL,
    "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT "ck_catalog_alias_approval" CHECK (("approval" = ANY (ARRAY['unconfirmed'::"text", 'confirmed'::"text"])))
);

CREATE SEQUENCE "__CATALOG_SCHEMA__"."catalog_aliases_alias_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "__CATALOG_SCHEMA__"."catalog_aliases_alias_id_seq" OWNED BY "__CATALOG_SCHEMA__"."catalog_aliases"."alias_id";

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_audiences" (
    "publication_id" bigint NOT NULL,
    "kind" "text" NOT NULL,
    "audience_type" "text",
    "min_age" integer,
    "max_age" integer,
    "position" integer NOT NULL,
    CONSTRAINT "ck_catalog_audience_age_range" CHECK ((("min_age" IS NULL) OR ("max_age" IS NULL) OR ("min_age" <= "max_age"))),
    CONSTRAINT "ck_catalog_audience_max_age" CHECK ((("max_age" IS NULL) OR ("max_age" >= 0))),
    CONSTRAINT "ck_catalog_audience_min_age" CHECK ((("min_age" IS NULL) OR ("min_age" >= 0)))
);

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_classification_nodes" (
    "node_id" bigint NOT NULL,
    "ddc" "text" NOT NULL,
    "parent_id" bigint,
    "label_en" "text" NOT NULL,
    "label_tt" "text",
    "revision" bigint DEFAULT '1'::bigint NOT NULL,
    "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT "ck_catalog_node_parent" CHECK ((("parent_id" IS NULL) OR ("parent_id" <> "node_id")))
);

CREATE SEQUENCE "__CATALOG_SCHEMA__"."catalog_classification_nodes_node_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "__CATALOG_SCHEMA__"."catalog_classification_nodes_node_id_seq" OWNED BY "__CATALOG_SCHEMA__"."catalog_classification_nodes"."node_id";

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_classifications" (
    "classification_id" bigint NOT NULL,
    "node_id" bigint NOT NULL,
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "revision" bigint DEFAULT '1'::bigint NOT NULL,
    "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "created_by" "text" DEFAULT 'gemini'::"text" NOT NULL,
    "created_at" timestamp without time zone
);

CREATE SEQUENCE "__CATALOG_SCHEMA__"."catalog_classifications_classification_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "__CATALOG_SCHEMA__"."catalog_classifications_classification_id_seq" OWNED BY "__CATALOG_SCHEMA__"."catalog_classifications"."classification_id";

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_collections" (
    "collection_id" bigint NOT NULL,
    "title" "text" NOT NULL,
    "notes" "text",
    "include_in_library" boolean DEFAULT true NOT NULL,
    "revision" bigint DEFAULT '1'::bigint NOT NULL,
    "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "metadata_template_json" "text" DEFAULT '{}'::"text" NOT NULL,
    "applied_at" "text",
    "created_at" "text",
    "normalized_title" "text",
    "source_updated_at" "text"
);

CREATE SEQUENCE "__CATALOG_SCHEMA__"."catalog_collections_collection_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "__CATALOG_SCHEMA__"."catalog_collections_collection_id_seq" OWNED BY "__CATALOG_SCHEMA__"."catalog_collections"."collection_id";

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_contributions" (
    "contribution_id" bigint NOT NULL,
    "publication_id" bigint NOT NULL,
    "name_id" bigint NOT NULL,
    "entity_id" bigint,
    "role" "text" NOT NULL,
    "position" integer NOT NULL,
    "nested_position" integer DEFAULT 0 NOT NULL,
    "resolution" "text" DEFAULT 'unconfirmed'::"text" NOT NULL,
    "revision" bigint DEFAULT '1'::bigint NOT NULL,
    "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT "ck_catalog_credit_entity" CHECK ((("resolution" <> 'confirmed'::"text") OR ("entity_id" IS NOT NULL))),
    CONSTRAINT "ck_catalog_credit_positions" CHECK ((("position" >= 0) AND ("nested_position" >= 0))),
    CONSTRAINT "ck_catalog_credit_resolution" CHECK (("resolution" = ANY (ARRAY['unconfirmed'::"text", 'confirmed'::"text"])))
);

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_credit_groups" (
    "publication_id" bigint NOT NULL,
    "role" "text" NOT NULL,
    "position" integer NOT NULL,
    "role_name" "text",
    CONSTRAINT "ck_catalog_credit_group_position" CHECK (("position" >= 0))
);

CREATE VIEW "__CATALOG_SCHEMA__"."catalog_contribution_metadata" AS
 SELECT "contribution_id",
    "publication_id",
    "name_id",
    "entity_id",
    "role",
    ( SELECT "catalog_credit_groups"."role_name"
           FROM "__CATALOG_SCHEMA__"."catalog_credit_groups"
          WHERE (("catalog_credit_groups"."publication_id" = "p"."publication_id") AND ("catalog_credit_groups"."role" = "p"."role") AND ("catalog_credit_groups"."position" = "p"."position"))) AS "role_name",
    "position",
    "nested_position",
    "resolution",
    "revision",
    "updated_at"
   FROM "__CATALOG_SCHEMA__"."catalog_contributions" "p";

CREATE SEQUENCE "__CATALOG_SCHEMA__"."catalog_contributions_contribution_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "__CATALOG_SCHEMA__"."catalog_contributions_contribution_id_seq" OWNED BY "__CATALOG_SCHEMA__"."catalog_contributions"."contribution_id";

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_document_access_modes" (
    "md5" "text" NOT NULL,
    "position" integer NOT NULL,
    "mode" "text" NOT NULL,
    CONSTRAINT "ck_catalog_access_mode_position" CHECK (("position" >= 0)),
    CONSTRAINT "ck_catalog_access_mode_value" CHECK (("mode" = ANY (ARRAY['auditory'::"text", 'tactile'::"text", 'textual'::"text", 'visual'::"text"])))
);

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_documents" (
    "md5" "text" NOT NULL,
    "publication_id" bigint NOT NULL,
    "mime_type" "text",
    "complete" boolean DEFAULT true NOT NULL,
    "restricted" boolean DEFAULT false NOT NULL,
    "selected" boolean DEFAULT true NOT NULL,
    "content_extraction_method" "text",
    "meta_extraction_method" "text",
    "revision" bigint DEFAULT '1'::bigint NOT NULL,
    "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE VIEW "__CATALOG_SCHEMA__"."catalog_document_metadata" AS
 SELECT "md5",
    "publication_id",
    "mime_type",
    "complete",
    "restricted",
    "selected",
    "content_extraction_method",
    "meta_extraction_method",
    ARRAY( SELECT "catalog_document_access_modes"."mode"
           FROM "__CATALOG_SCHEMA__"."catalog_document_access_modes"
          WHERE ("catalog_document_access_modes"."md5" = "p"."md5")
          ORDER BY "catalog_document_access_modes"."position") AS "access_modes",
    "revision",
    "updated_at"
   FROM "__CATALOG_SCHEMA__"."catalog_documents" "p";

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_entities" (
    "entity_id" bigint NOT NULL,
    "kind" "text" NOT NULL,
    "display_name" "text" NOT NULL,
    "approval" "text" DEFAULT 'unconfirmed'::"text" NOT NULL,
    "status" "text" DEFAULT 'active'::"text" NOT NULL,
    "merged_into_id" bigint,
    "surname_full" "text",
    "surname_initials" "text",
    "name_full" "text",
    "name_initials" "text",
    "father_name_full" "text",
    "father_name_initials" "text",
    "title" "text",
    "sex" "text",
    "identity_key" "text",
    "notes" "text",
    "revision" bigint DEFAULT '1'::bigint NOT NULL,
    "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "normalized_name" "text",
    "created_at" "text",
    "source_updated_at" "text",
    CONSTRAINT "catalog_entities_approval_check" CHECK (("approval" = ANY (ARRAY['unconfirmed'::"text", 'confirmed'::"text"]))),
    CONSTRAINT "catalog_entities_kind_check" CHECK (("kind" = ANY (ARRAY['person'::"text", 'organization'::"text", 'unknown'::"text"]))),
    CONSTRAINT "catalog_entities_status_check" CHECK (("status" = ANY (ARRAY['active'::"text", 'merged'::"text", 'archived'::"text"]))),
    CONSTRAINT "ck_catalog_entity_merge_status" CHECK ((("status" = 'merged'::"text") = ("merged_into_id" IS NOT NULL))),
    CONSTRAINT "ck_catalog_entity_merge_target" CHECK ((("merged_into_id" IS NULL) OR ("merged_into_id" <> "entity_id")))
);

CREATE SEQUENCE "__CATALOG_SCHEMA__"."catalog_entities_entity_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "__CATALOG_SCHEMA__"."catalog_entities_entity_id_seq" OWNED BY "__CATALOG_SCHEMA__"."catalog_entities"."entity_id";

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_entity_roles" (
    "entity_id" bigint NOT NULL,
    "role" "text" NOT NULL
);

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_evidence" (
    "evidence_id" bigint NOT NULL,
    "md5" "text",
    "record_kind" "text" NOT NULL,
    "record_key" "text" NOT NULL,
    "source" "text" NOT NULL,
    "payload" "jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE SEQUENCE "__CATALOG_SCHEMA__"."catalog_evidence_evidence_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "__CATALOG_SCHEMA__"."catalog_evidence_evidence_id_seq" OWNED BY "__CATALOG_SCHEMA__"."catalog_evidence"."evidence_id";

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_genres" (
    "publication_id" bigint NOT NULL,
    "value" "text" NOT NULL,
    "position" integer NOT NULL
);

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_identifiers" (
    "publication_id" bigint NOT NULL,
    "kind" "text" NOT NULL,
    "value" "text" NOT NULL,
    "normalized" "text" NOT NULL,
    "position" integer NOT NULL
);

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_imports" (
    "import_id" bigint NOT NULL,
    "source_fingerprint" "text" NOT NULL,
    "manifest" "jsonb" NOT NULL,
    "state" "text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE SEQUENCE "__CATALOG_SCHEMA__"."catalog_imports_import_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "__CATALOG_SCHEMA__"."catalog_imports_import_id_seq" OWNED BY "__CATALOG_SCHEMA__"."catalog_imports"."import_id";

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_locations" (
    "location_id" bigint NOT NULL,
    "md5" "text" NOT NULL,
    "provider" "text" NOT NULL,
    "purpose" "text" NOT NULL,
    "locator" "text",
    "source_path" "text",
    "resource_id" "text",
    "public_url" "text",
    "public_key" "text",
    "size" bigint,
    "etag" "text",
    "verified_at" timestamp with time zone,
    "revision" bigint DEFAULT '1'::bigint NOT NULL,
    "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT "catalog_locations_size_check" CHECK ((("size" IS NULL) OR ("size" >= 0)))
);

CREATE SEQUENCE "__CATALOG_SCHEMA__"."catalog_locations_location_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "__CATALOG_SCHEMA__"."catalog_locations_location_id_seq" OWNED BY "__CATALOG_SCHEMA__"."catalog_locations"."location_id";

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_names" (
    "name_id" bigint NOT NULL,
    "kind" "text" NOT NULL,
    "raw_name" "text" NOT NULL,
    CONSTRAINT "ck_catalog_name_kind" CHECK (("kind" = ANY (ARRAY['person'::"text", 'organization'::"text", 'unknown'::"text"])))
);

CREATE SEQUENCE "__CATALOG_SCHEMA__"."catalog_names_name_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "__CATALOG_SCHEMA__"."catalog_names_name_id_seq" OWNED BY "__CATALOG_SCHEMA__"."catalog_names"."name_id";

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_preview_pages" (
    "request_id" bigint NOT NULL,
    "role" "text" NOT NULL,
    "page_number" integer NOT NULL,
    "small_key" "text",
    "large_key" "text"
);

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_preview_requests" (
    "request_id" bigint NOT NULL,
    "md5" "text" NOT NULL,
    "idempotency_key" "text" NOT NULL,
    "recipe" "text" NOT NULL,
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "private" boolean NOT NULL,
    "actor" "text" NOT NULL,
    "claim_token" "text",
    "lease_until" timestamp with time zone,
    "source_page_count" integer,
    "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE SEQUENCE "__CATALOG_SCHEMA__"."catalog_preview_requests_request_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "__CATALOG_SCHEMA__"."catalog_preview_requests_request_id_seq" OWNED BY "__CATALOG_SCHEMA__"."catalog_preview_requests"."request_id";

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_proposal_members" (
    "proposal_id" bigint NOT NULL,
    "entity_id" bigint,
    "name_id" bigint,
    "reviewed_revision" bigint,
    "snapshot" "jsonb" NOT NULL,
    "member_id" bigint NOT NULL,
    CONSTRAINT "catalog_proposal_members_check" CHECK ((("entity_id" IS NULL) <> ("name_id" IS NULL)))
);

CREATE SEQUENCE "__CATALOG_SCHEMA__"."catalog_proposal_members_member_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "__CATALOG_SCHEMA__"."catalog_proposal_members_member_id_seq" OWNED BY "__CATALOG_SCHEMA__"."catalog_proposal_members"."member_id";

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_proposals" (
    "proposal_id" bigint NOT NULL,
    "kind" "text" NOT NULL,
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "display_name" "text",
    "evidence" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "field_changes" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "publication_id" bigint,
    "revision" bigint DEFAULT '1'::bigint NOT NULL,
    "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE SEQUENCE "__CATALOG_SCHEMA__"."catalog_proposals_proposal_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "__CATALOG_SCHEMA__"."catalog_proposals_proposal_id_seq" OWNED BY "__CATALOG_SCHEMA__"."catalog_proposals"."proposal_id";

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_protections" (
    "record_kind" "text" NOT NULL,
    "record_key" "text" NOT NULL,
    "field" "text" NOT NULL,
    "actor" "text" NOT NULL
);

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_publication_languages" (
    "publication_id" bigint NOT NULL,
    "position" integer NOT NULL,
    "language" "text" NOT NULL,
    CONSTRAINT "ck_catalog_language_position" CHECK (("position" >= 0)),
    CONSTRAINT "ck_catalog_language_value" CHECK (("btrim"("language") <> ''::"text"))
);

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_publications" (
    "publication_id" bigint NOT NULL,
    "name" "text",
    "work_type" "text",
    "description" "text",
    "edition" "text",
    "date_published" "text",
    "page_count" integer,
    "audience_array" boolean DEFAULT false NOT NULL,
    "inclusion" "text" DEFAULT 'pending'::"text" NOT NULL,
    "evaluation_method" "text",
    "classification_id" bigint,
    "collection_id" bigint,
    "merged_into_id" bigint,
    "revision" bigint DEFAULT '1'::bigint NOT NULL,
    "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "collection_item_title" "text",
    "collection_created_at" "text",
    "collection_updated_at" "text",
    "has_metadata" boolean DEFAULT true NOT NULL,
    "metadata_present" boolean DEFAULT true NOT NULL,
    CONSTRAINT "catalog_publications_inclusion_check" CHECK (("inclusion" = ANY (ARRAY['pending'::"text", 'included'::"text", 'excluded'::"text"]))),
    CONSTRAINT "catalog_publications_page_count_check" CHECK ((("page_count" IS NULL) OR ("page_count" > 0))),
    CONSTRAINT "ck_catalog_publication_merge_target" CHECK ((("merged_into_id" IS NULL) OR ("merged_into_id" <> "publication_id")))
);

CREATE VIEW "__CATALOG_SCHEMA__"."catalog_publication_metadata" AS
 SELECT "publication_id",
    "name",
    "work_type",
    "description",
    "edition",
    "date_published",
    "page_count",
    ARRAY( SELECT "catalog_publication_languages"."language"
           FROM "__CATALOG_SCHEMA__"."catalog_publication_languages"
          WHERE ("catalog_publication_languages"."publication_id" = "p"."publication_id")
          ORDER BY "catalog_publication_languages"."position") AS "languages",
    "audience_array",
    "inclusion",
    "evaluation_method",
    "classification_id",
    "collection_id",
    "merged_into_id",
    "revision",
    "updated_at",
    "collection_item_title",
    "collection_created_at",
    "collection_updated_at",
    "has_metadata",
    "metadata_present"
   FROM "__CATALOG_SCHEMA__"."catalog_publications" "p";

CREATE SEQUENCE "__CATALOG_SCHEMA__"."catalog_publications_publication_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "__CATALOG_SCHEMA__"."catalog_publications_publication_id_seq" OWNED BY "__CATALOG_SCHEMA__"."catalog_publications"."publication_id";

CREATE VIEW "__CATALOG_SCHEMA__"."catalog_publisher_proposals" AS
 SELECT "c"."proposal_id",
    (("v"."payload" ->> 'analysis_id'::"text"))::bigint AS "analysis_id",
    ("v"."payload" ->> 'fingerprint'::"text") AS "fingerprint",
    ("v"."payload" -> 'proposal'::"text") AS "proposal",
    ("v"."payload" -> 'members'::"text") AS "members",
        CASE "c"."status"
            WHEN 'pending'::"text" THEN COALESCE(("v"."payload" ->> 'status'::"text"), 'pending'::"text")
            WHEN 'deferred'::"text" THEN 'skipped'::"text"
            ELSE "c"."status"
        END AS "status",
    ("v"."payload" ->> 'created_at'::"text") AS "created_at",
    ("v"."payload" ->> 'updated_at'::"text") AS "updated_at",
    ("v"."payload" -> 'review_edit'::"text") AS "review_edit"
   FROM ("__CATALOG_SCHEMA__"."catalog_proposals" "c"
     CROSS JOIN LATERAL ( SELECT (COALESCE(("c"."evidence" -> 'publisher'::"text"), ("c"."evidence" -> 'original'::"text"), '{}'::"jsonb") || COALESCE(("c"."field_changes" -> 'publisher'::"text"), '{}'::"jsonb")) AS "payload") "v")
  WHERE ((("c"."evidence" ->> 'source'::"text") = ANY (ARRAY['legacy.publisher_merge_proposals'::"text", 'catalog.publisher_analysis'::"text"])) AND ((("c"."field_changes" -> 'publisher'::"text") ->> 'discarded'::"text") IS DISTINCT FROM 'true'::"text"));

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_reference_authors" (
    "publication_id" bigint NOT NULL,
    "kind" "text",
    "name" "text",
    "position" integer NOT NULL
);

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_reference_urls" (
    "publication_id" bigint NOT NULL,
    "position" integer NOT NULL,
    "url" "text" NOT NULL,
    CONSTRAINT "ck_catalog_reference_url_position" CHECK (("position" >= 0))
);

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_references" (
    "publication_id" bigint NOT NULL,
    "work_type" "text",
    "name" "text",
    "language" "text",
    "urls_present" boolean DEFAULT false NOT NULL
);

CREATE VIEW "__CATALOG_SCHEMA__"."catalog_reference_metadata" AS
 SELECT "publication_id",
    "work_type",
    "name",
    "language",
        CASE
            WHEN "urls_present" THEN ARRAY( SELECT "catalog_reference_urls"."url"
               FROM "__CATALOG_SCHEMA__"."catalog_reference_urls"
              WHERE ("catalog_reference_urls"."publication_id" = "p"."publication_id")
              ORDER BY "catalog_reference_urls"."position")
            ELSE NULL::"text"[]
        END AS "urls"
   FROM "__CATALOG_SCHEMA__"."catalog_references" "p";

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_revisions" (
    "revision_id" bigint NOT NULL,
    "record_kind" "text" NOT NULL,
    "record_key" "text" NOT NULL,
    "actor" "text" NOT NULL,
    "before" "jsonb",
    "after" "jsonb",
    "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE SEQUENCE "__CATALOG_SCHEMA__"."catalog_revisions_revision_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "__CATALOG_SCHEMA__"."catalog_revisions_revision_id_seq" OWNED BY "__CATALOG_SCHEMA__"."catalog_revisions"."revision_id";

CREATE VIEW "__CATALOG_SCHEMA__"."catalog_selected_previews" AS
 SELECT "r"."md5",
    "r"."request_id",
    "r"."recipe" AS "recipe_version",
    "r"."status",
    "r"."source_page_count",
    "r"."private",
    ( SELECT "p"."page_number"
           FROM "__CATALOG_SCHEMA__"."catalog_preview_pages" "p"
          WHERE (("p"."request_id" = "r"."request_id") AND ("p"."role" = 'first'::"text"))) AS "first_preview_page",
    ( SELECT "p"."page_number"
           FROM "__CATALOG_SCHEMA__"."catalog_preview_pages" "p"
          WHERE (("p"."request_id" = "r"."request_id") AND ("p"."role" = 'second'::"text"))) AS "second_preview_page",
    ( SELECT "p"."page_number"
           FROM "__CATALOG_SCHEMA__"."catalog_preview_pages" "p"
          WHERE (("p"."request_id" = "r"."request_id") AND ("p"."role" = 'last'::"text"))) AS "last_preview_page"
   FROM ("__CATALOG_SCHEMA__"."catalog_preview_requests" "r"
     JOIN "__CATALOG_SCHEMA__"."catalog_documents" "d" USING ("md5"))
  WHERE (("r"."status" = 'ready'::"text") AND ((NOT "d"."restricted") OR "r"."private") AND (NOT (EXISTS ( SELECT 1
           FROM "__CATALOG_SCHEMA__"."catalog_preview_requests" "newer"
          WHERE (("newer"."md5" = "r"."md5") AND ("newer"."status" = 'ready'::"text") AND ((NOT "d"."restricted") OR "newer"."private") AND ("newer"."request_id" > "r"."request_id"))))));

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_separations" (
    "left_key" "text" NOT NULL,
    "right_key" "text" NOT NULL,
    "actor" "text" NOT NULL,
    CONSTRAINT "catalog_separations_check" CHECK (("left_key" < "right_key"))
);

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_subjects" (
    "publication_id" bigint NOT NULL,
    "name" "text",
    "term_code" "text",
    "set_name" "text",
    "set_url" "text",
    "set_is_url" boolean NOT NULL,
    "position" integer NOT NULL
);

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_sufficient_mode_items" (
    "md5" "text" NOT NULL,
    "group_position" integer NOT NULL,
    "position" integer NOT NULL,
    "mode" "text" NOT NULL,
    CONSTRAINT "ck_catalog_sufficient_mode_positions" CHECK ((("group_position" >= 0) AND ("position" >= 0))),
    CONSTRAINT "ck_catalog_sufficient_mode_value" CHECK (("mode" = ANY (ARRAY['auditory'::"text", 'tactile'::"text", 'textual'::"text", 'visual'::"text"])))
);

CREATE TABLE "__CATALOG_SCHEMA__"."catalog_sufficient_modes" (
    "md5" "text" NOT NULL,
    "position" integer NOT NULL,
    CONSTRAINT "ck_catalog_sufficient_group_position" CHECK (("position" >= 0))
);

CREATE VIEW "__CATALOG_SCHEMA__"."catalog_sufficient_mode_metadata" AS
 SELECT "md5",
    ARRAY( SELECT "catalog_sufficient_mode_items"."mode"
           FROM "__CATALOG_SCHEMA__"."catalog_sufficient_mode_items"
          WHERE (("catalog_sufficient_mode_items"."md5" = "p"."md5") AND ("catalog_sufficient_mode_items"."group_position" = "p"."position"))
          ORDER BY "catalog_sufficient_mode_items"."position") AS "modes",
    "position"
   FROM "__CATALOG_SCHEMA__"."catalog_sufficient_modes" "p";

CREATE TABLE "__CATALOG_SCHEMA__"."document_cleanup_queue" (
    "cleanup_id" bigint NOT NULL,
    "scope" "text" NOT NULL,
    "action" "text" NOT NULL,
    "reason" "text" NOT NULL,
    "md5" "text" NOT NULL,
    "source_resource_id" "text",
    "source_path" "text" NOT NULL,
    "target_path" "text",
    "status" "text" DEFAULT 'planned'::"text" NOT NULL,
    "phase" "text" DEFAULT 'planned'::"text" NOT NULL,
    "evidence_json" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "completed_at" timestamp with time zone,
    CONSTRAINT "document_cleanup_queue_action_check" CHECK (("action" = ANY (ARRAY['move'::"text", 'delete'::"text"]))),
    CONSTRAINT "document_cleanup_queue_scope_check" CHECK (("scope" = ANY (ARRAY['document'::"text", 'duplicate_resource'::"text", 'source_resource'::"text"]))),
    CONSTRAINT "document_cleanup_queue_status_check" CHECK (("status" = ANY (ARRAY['planned'::"text", 'running'::"text", 'completed'::"text", 'failed'::"text", 'canceled'::"text", 'recovered'::"text"])))
);

ALTER TABLE "__CATALOG_SCHEMA__"."document_cleanup_queue" ALTER COLUMN "cleanup_id" ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME "__CATALOG_SCHEMA__"."document_cleanup_queue_cleanup_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE "__CATALOG_SCHEMA__"."library_collection_events" (
    "event_id" bigint NOT NULL,
    "action" "text" NOT NULL,
    "payload_json" "text" NOT NULL,
    "created_at" "text" NOT NULL
);

ALTER TABLE "__CATALOG_SCHEMA__"."library_collection_events" ALTER COLUMN "event_id" ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME "__CATALOG_SCHEMA__"."library_collection_events_event_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE VIEW "__CATALOG_SCHEMA__"."library_collection_items" AS
 SELECT "p"."collection_id",
    "d"."md5",
    "p"."collection_item_title" AS "item_title",
    COALESCE("p"."collection_created_at", ("p"."updated_at")::"text") AS "created_at",
    COALESCE("p"."collection_updated_at", ("p"."updated_at")::"text") AS "updated_at"
   FROM ("__CATALOG_SCHEMA__"."catalog_document_metadata" "d"
     JOIN "__CATALOG_SCHEMA__"."catalog_publication_metadata" "p" USING ("publication_id"))
  WHERE ("p"."collection_id" IS NOT NULL);

CREATE TABLE "__CATALOG_SCHEMA__"."library_collection_proposal_items" (
    "proposal_id" bigint NOT NULL,
    "md5" "text" NOT NULL,
    "input_hash" "text" NOT NULL,
    "deterministic_score" double precision DEFAULT 0.0 NOT NULL,
    "evidence_json" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "gemini_verdict" "text",
    "gemini_confidence" double precision,
    "gemini_rationale" "text",
    "gemini_model" "text",
    "prompt_version" "text",
    "validated_at" "text",
    "decision" "text"
);

CREATE TABLE "__CATALOG_SCHEMA__"."library_collection_proposals" (
    "proposal_id" bigint NOT NULL,
    "proposal_key" "text" NOT NULL,
    "proposal_type" "text" NOT NULL,
    "target_collection_id" bigint,
    "proposed_title" "text" NOT NULL,
    "normalized_title" "text" NOT NULL,
    "status" "text" NOT NULL,
    "detector_version" "text" NOT NULL,
    "deterministic_score" double precision DEFAULT 0.0 NOT NULL,
    "evidence_json" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "gemini_collection_verdict" boolean,
    "gemini_canonical_name" "text",
    "gemini_confidence" double precision,
    "gemini_rationale" "text",
    "created_at" "text" NOT NULL,
    "updated_at" "text" NOT NULL,
    "reviewed_at" "text",
    "superseded_at" "text"
);

ALTER TABLE "__CATALOG_SCHEMA__"."library_collection_proposals" ALTER COLUMN "proposal_id" ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME "__CATALOG_SCHEMA__"."library_collection_proposals_proposal_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE "__CATALOG_SCHEMA__"."library_collection_signatures" (
    "signature_id" bigint NOT NULL,
    "collection_id" bigint NOT NULL,
    "signature_type" "text" NOT NULL,
    "normalized_value" "text" NOT NULL,
    "provenance" "text" NOT NULL,
    "created_at" "text" NOT NULL,
    "updated_at" "text" NOT NULL
);

ALTER TABLE "__CATALOG_SCHEMA__"."library_collection_signatures" ALTER COLUMN "signature_id" ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME "__CATALOG_SCHEMA__"."library_collection_signatures_signature_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE VIEW "__CATALOG_SCHEMA__"."library_collections" AS
 SELECT "collection_id",
    "title",
    COALESCE("normalized_title", "__CATALOG_SCHEMA__"."catalog_title_key"("title")) AS "normalized_title",
    (("include_in_library")::integer)::bigint AS "include_in_library",
    "metadata_template_json",
    "notes",
    "applied_at",
    COALESCE("created_at", ("updated_at")::"text") AS "created_at",
        CASE
            WHEN (("revision" = 1) AND ("source_updated_at" IS NOT NULL)) THEN "source_updated_at"
            ELSE ("updated_at")::"text"
        END AS "updated_at"
   FROM "__CATALOG_SCHEMA__"."catalog_collections" "c";

CREATE TABLE "__CATALOG_SCHEMA__"."library_isbn_duplicate_reviews" (
    "review_id" bigint NOT NULL,
    "isbn" "text" NOT NULL,
    "candidate_hash" "text" NOT NULL,
    "candidates_json" "jsonb" NOT NULL,
    "evidence_json" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "keep_md5s_json" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "decided_at" timestamp with time zone,
    CONSTRAINT "library_isbn_duplicate_reviews_status_check" CHECK (("status" = ANY (ARRAY['pending'::"text", 'decided'::"text", 'superseded'::"text"])))
);

ALTER TABLE "__CATALOG_SCHEMA__"."library_isbn_duplicate_reviews" ALTER COLUMN "review_id" ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME "__CATALOG_SCHEMA__"."library_isbn_duplicate_reviews_review_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE "__CATALOG_SCHEMA__"."library_non_pdf_extraction_state" (
    "md5" "text" NOT NULL,
    "extractor_version" "text" NOT NULL,
    "detected_format" "text",
    "status" "text" NOT NULL,
    "generated_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "updated_at" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    "verified_source_mime" "text",
    "decision_reason" "text",
    CONSTRAINT "ck_non_pdf_durable_state" CHECK (("status" = ANY (ARRAY['ready'::"text", 'unsupported'::"text", 'detected'::"text"])))
);

CREATE TABLE "__CATALOG_SCHEMA__"."library_upstream_metadata" (
    "md5" "text" NOT NULL,
    "payload_json" "jsonb" NOT NULL,
    CONSTRAINT "library_upstream_metadata_md5_check" CHECK (("md5" ~ '^[0-9a-f]{32}$'::"text")),
    CONSTRAINT "library_upstream_metadata_payload_json_check" CHECK (("jsonb_typeof"("payload_json") = 'object'::"text"))
);

CREATE VIEW "__CATALOG_SCHEMA__"."normalization_aliases" AS
 SELECT "r"."alias_id",
    "r"."entity_type",
    "n"."raw_name",
    "r"."normalized_name",
    "r"."script_label",
    "r"."docs_count",
    "r"."mentions_count",
    "r"."marker_count",
    "r"."decision_status",
    "r"."entity_id" AS "canonical_id",
    "r"."confidence",
    "r"."source",
    "r"."reason",
    "r"."created_at",
    "r"."updated_at",
    "r"."surname_full",
    "r"."surname_initials",
    "r"."name_full",
    "r"."name_initials",
    "r"."father_name_full",
    "r"."father_name_initials",
    "r"."title",
    "r"."sex",
    "r"."source_roles",
    "r"."successful_model",
    "r"."prompt_version",
    "r"."schema_version"
   FROM ("__CATALOG_SCHEMA__"."catalog_alias_reviews" "r"
     JOIN "__CATALOG_SCHEMA__"."catalog_names" "n" USING ("name_id"));

CREATE VIEW "__CATALOG_SCHEMA__"."normalization_canonicals" AS
 SELECT "entity_id" AS "canonical_id",
        CASE
            WHEN (("kind" <> 'person'::"text") OR (EXISTS ( SELECT 1
               FROM "__CATALOG_SCHEMA__"."catalog_entity_roles" "r"
              WHERE (("r"."entity_id" = "e"."entity_id") AND ("r"."role" = 'publisher'::"text")))) OR (EXISTS ( SELECT 1
               FROM "__CATALOG_SCHEMA__"."catalog_contribution_metadata" "c"
              WHERE (("c"."entity_id" = "e"."entity_id") AND ("c"."role" = 'publisher'::"text"))))) THEN 'publisher'::"text"
            ELSE 'personality'::"text"
        END AS "entity_type",
    "display_name",
    COALESCE("normalized_name", "__CATALOG_SCHEMA__"."catalog_name_key"("display_name")) AS "normalized_name",
    "status",
    "merged_into_id",
    "notes",
    COALESCE("created_at", ("updated_at")::"text") AS "created_at",
        CASE
            WHEN (("revision" = 1) AND ("source_updated_at" IS NOT NULL)) THEN "source_updated_at"
            ELSE ("updated_at")::"text"
        END AS "updated_at",
    "surname_full",
    "surname_initials",
    "name_full",
    "name_initials",
    "father_name_full",
    "father_name_initials",
    "title",
    "sex",
    "identity_key"
   FROM "__CATALOG_SCHEMA__"."catalog_entities" "e";

CREATE TABLE "__CATALOG_SCHEMA__"."normalization_events" (
    "event_id" bigint NOT NULL,
    "entity_type" "text" NOT NULL,
    "action" "text" NOT NULL,
    "payload_json" "text" NOT NULL,
    "reverted" bigint DEFAULT 0 NOT NULL,
    "created_at" "text" NOT NULL
);

ALTER TABLE "__CATALOG_SCHEMA__"."normalization_events" ALTER COLUMN "event_id" ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME "__CATALOG_SCHEMA__"."normalization_events_event_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE "__CATALOG_SCHEMA__"."normalization_suggestions" (
    "suggestion_id" bigint NOT NULL,
    "entity_type" "text" NOT NULL,
    "raw_name" "text" NOT NULL,
    "normalized_name" "text" NOT NULL,
    "target_canonical_id" bigint,
    "suggestion_kind" "text" NOT NULL,
    "confidence" double precision NOT NULL,
    "confidence_band" "text" NOT NULL,
    "model" "text",
    "rationale" "text",
    "status" "text" DEFAULT 'open'::"text" NOT NULL,
    "created_at" "text" NOT NULL,
    "updated_at" "text" NOT NULL
);

ALTER TABLE "__CATALOG_SCHEMA__"."normalization_suggestions" ALTER COLUMN "suggestion_id" ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME "__CATALOG_SCHEMA__"."normalization_suggestions_suggestion_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE "__CATALOG_SCHEMA__"."personality_normalization_checkpoints" (
    "raw_name" "text" NOT NULL,
    "source_fingerprint" "text" CONSTRAINT "personality_normalization_checkpoin_source_fingerprint_not_null" NOT NULL,
    "document_count" bigint DEFAULT 0 NOT NULL,
    "mention_count" bigint DEFAULT 0 NOT NULL,
    "source_roles" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "prompt_version" "text" NOT NULL,
    "schema_version" "text" NOT NULL,
    "state" "text" NOT NULL,
    "decision_reason" "text",
    "canonical_id" bigint,
    "updated_at" "text" NOT NULL,
    "completed_at" "text",
    "successful_model" "text",
    "decision_evidence" "jsonb" DEFAULT '{}'::"jsonb" CONSTRAINT "personality_normalization_checkpoint_decision_evidence_not_null" NOT NULL,
    CONSTRAINT "ck_personality_durable_state" CHECK (("state" = ANY (ARRAY['succeeded'::"text", 'not_person'::"text", 'unusable'::"text", 'retry_requested'::"text", 'failed'::"text"])))
);

CREATE TABLE "__CATALOG_SCHEMA__"."publisher_merge_analyses" (
    "analysis_id" bigint NOT NULL,
    "fingerprint" "text" NOT NULL,
    "scope" "text" NOT NULL,
    "inventory" "jsonb" NOT NULL,
    "metadata" "jsonb" NOT NULL,
    "response" "jsonb",
    "state" "text" NOT NULL,
    "created_at" "text" NOT NULL,
    "updated_at" "text" NOT NULL,
    CONSTRAINT "publisher_merge_analyses_state_check" CHECK (("state" = ANY (ARRAY['generating'::"text", 'checkpointed'::"text", 'imported'::"text", 'failed'::"text"])))
);

CREATE SEQUENCE "__CATALOG_SCHEMA__"."publisher_merge_analyses_analysis_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE "__CATALOG_SCHEMA__"."publisher_merge_analyses_analysis_id_seq" OWNED BY "__CATALOG_SCHEMA__"."publisher_merge_analyses"."analysis_id";

CREATE TABLE "__CATALOG_SCHEMA__"."publisher_review_draft" (
    "singleton" integer NOT NULL,
    "revision" bigint DEFAULT 0 NOT NULL,
    "changes" "jsonb" NOT NULL,
    "reviewed" "jsonb" NOT NULL,
    "proposal_ids" "jsonb" NOT NULL,
    "updated_at" "text" NOT NULL,
    CONSTRAINT "publisher_review_draft_singleton_check" CHECK (("singleton" = 1))
);

CREATE TABLE "__CATALOG_SCHEMA__"."publisher_separations" (
    "left_key" "text" NOT NULL,
    "right_key" "text" NOT NULL,
    "provenance" "jsonb" NOT NULL,
    "created_at" "text" NOT NULL,
    CONSTRAINT "publisher_separations_check" CHECK (("left_key" < "right_key"))
);

CREATE VIEW "public"."classification" AS
 SELECT "c"."classification_id" AS "id",
    ("path"."data" ->> 'ddc'::"text") AS "ddc",
    (("path"."data" -> 'path_en'::"text"))::json AS "path_en",
    ( SELECT "lower"("string_agg"("jsonb_array_elements_text"."value", '|'::"text" ORDER BY "jsonb_array_elements_text"."ordinality")) AS "lower"
           FROM "jsonb_array_elements_text"(("path"."data" -> 'path_en'::"text")) WITH ORDINALITY "jsonb_array_elements_text"("value", "ordinality")) AS "path_en_key",
    (NULLIF(("path"."data" -> 'path_tt'::"text"), 'null'::"jsonb"))::json AS "path_tt",
    "c"."status",
    "c"."created_by",
    COALESCE("c"."created_at", (CURRENT_TIMESTAMP)::timestamp without time zone) AS "created_at"
   FROM ("__CATALOG_SCHEMA__"."catalog_classifications" "c"
     CROSS JOIN LATERAL ( SELECT "__CATALOG_SCHEMA__"."catalog_path"("c"."node_id") AS "data") "path");

CREATE VIEW "public"."document" AS
 SELECT "d"."md5",
    "d"."mime_type",
    ( SELECT "catalog_locations"."source_path"
           FROM "__CATALOG_SCHEMA__"."catalog_locations"
          WHERE (("catalog_locations"."md5" = "d"."md5") AND ("catalog_locations"."provider" = 'yandex'::"text") AND ("catalog_locations"."purpose" = 'source'::"text"))) AS "ya_path",
        CASE
            WHEN (NOT "d"."restricted") THEN ( SELECT "catalog_locations"."public_url"
               FROM "__CATALOG_SCHEMA__"."catalog_locations"
              WHERE (("catalog_locations"."md5" = "d"."md5") AND ("catalog_locations"."provider" = 'yandex'::"text") AND ("catalog_locations"."purpose" = 'source'::"text")))
            ELSE NULL::"text"
        END AS "ya_public_url",
        CASE
            WHEN (NOT "d"."restricted") THEN ( SELECT "catalog_locations"."public_key"
               FROM "__CATALOG_SCHEMA__"."catalog_locations"
              WHERE (("catalog_locations"."md5" = "d"."md5") AND ("catalog_locations"."provider" = 'yandex'::"text") AND ("catalog_locations"."purpose" = 'source'::"text")))
            ELSE NULL::"text"
        END AS "ya_public_key",
    ( SELECT "catalog_locations"."resource_id"
           FROM "__CATALOG_SCHEMA__"."catalog_locations"
          WHERE (("catalog_locations"."md5" = "d"."md5") AND ("catalog_locations"."provider" = 'yandex'::"text") AND ("catalog_locations"."purpose" = 'source'::"text"))) AS "ya_resource_id",
    NULLIF("array_to_string"("p"."languages", ','::"text"), ''::"text") AS "language",
    "d"."content_extraction_method",
    "d"."meta_extraction_method",
    "d"."complete" AS "full",
    "d"."restricted" AS "sharing_restricted",
    ( SELECT "catalog_locations"."locator"
           FROM "__CATALOG_SCHEMA__"."catalog_locations"
          WHERE (("catalog_locations"."md5" = "d"."md5") AND ("catalog_locations"."provider" = 's3'::"text") AND ("catalog_locations"."purpose" = 'primary'::"text"))) AS "document_url",
    ( SELECT "catalog_locations"."locator"
           FROM "__CATALOG_SCHEMA__"."catalog_locations"
          WHERE (("catalog_locations"."md5" = "d"."md5") AND ("catalog_locations"."provider" = 's3'::"text") AND ("catalog_locations"."purpose" = 'content'::"text"))) AS "content_url",
    ( SELECT "catalog_locations"."size"
           FROM "__CATALOG_SCHEMA__"."catalog_locations"
          WHERE (("catalog_locations"."md5" = "d"."md5") AND ("catalog_locations"."provider" = 's3'::"text") AND ("catalog_locations"."purpose" = 'primary'::"text"))) AS "primary_storage_size",
    ( SELECT "catalog_locations"."etag"
           FROM "__CATALOG_SCHEMA__"."catalog_locations"
          WHERE (("catalog_locations"."md5" = "d"."md5") AND ("catalog_locations"."provider" = 's3'::"text") AND ("catalog_locations"."purpose" = 'primary'::"text"))) AS "primary_storage_etag",
    ( SELECT "catalog_locations"."verified_at"
           FROM "__CATALOG_SCHEMA__"."catalog_locations"
          WHERE (("catalog_locations"."md5" = "d"."md5") AND ("catalog_locations"."provider" = 's3'::"text") AND ("catalog_locations"."purpose" = 'primary'::"text"))) AS "primary_storage_verified_at"
   FROM ("__CATALOG_SCHEMA__"."catalog_document_metadata" "d"
     JOIN "__CATALOG_SCHEMA__"."catalog_publication_metadata" "p" USING ("publication_id"));

CREATE TABLE "public"."isbn_keep_many" (
    "isbn_key" character varying NOT NULL,
    "md5" character varying NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);

CREATE VIEW "public"."metadata" AS
 SELECT "d"."md5",
        CASE
            WHEN "p"."metadata_present" THEN ("__CATALOG_SCHEMA__"."catalog_schema_org"("d"."md5"))::json
            ELSE NULL::json
        END AS "schema_org",
        CASE "p"."inclusion"
            WHEN 'included'::"text" THEN true
            WHEN 'excluded'::"text" THEN false
            ELSE NULL::boolean
        END AS "lib",
    "p"."evaluation_method" AS "lib_eval_method",
    "p"."classification_id"
   FROM ("__CATALOG_SCHEMA__"."catalog_document_metadata" "d"
     JOIN "__CATALOG_SCHEMA__"."catalog_publication_metadata" "p" USING ("publication_id"))
  WHERE "p"."has_metadata";

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_alias_reviews" ALTER COLUMN "alias_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_alias_reviews_alias_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_aliases" ALTER COLUMN "alias_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_aliases_alias_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_classification_nodes" ALTER COLUMN "node_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_classification_nodes_node_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_classifications" ALTER COLUMN "classification_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_classifications_classification_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_collections" ALTER COLUMN "collection_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_collections_collection_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_contributions" ALTER COLUMN "contribution_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_contributions_contribution_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_entities" ALTER COLUMN "entity_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_entities_entity_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_evidence" ALTER COLUMN "evidence_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_evidence_evidence_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_imports" ALTER COLUMN "import_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_imports_import_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_locations" ALTER COLUMN "location_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_locations_location_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_names" ALTER COLUMN "name_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_names_name_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_preview_requests" ALTER COLUMN "request_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_preview_requests_request_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_proposal_members" ALTER COLUMN "member_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_proposal_members_member_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_proposals" ALTER COLUMN "proposal_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_proposals_proposal_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_publications" ALTER COLUMN "publication_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_publications_publication_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_revisions" ALTER COLUMN "revision_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_revisions_revision_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_collections" ALTER COLUMN "collection_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_collections_collection_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_collections" ALTER COLUMN "include_in_library" SET DEFAULT 1;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_collections" ALTER COLUMN "metadata_template_json" SET DEFAULT '{}'::"text";

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."normalization_aliases" ALTER COLUMN "alias_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_alias_reviews_alias_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."normalization_aliases" ALTER COLUMN "docs_count" SET DEFAULT 0;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."normalization_aliases" ALTER COLUMN "mentions_count" SET DEFAULT 0;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."normalization_aliases" ALTER COLUMN "marker_count" SET DEFAULT 0;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."normalization_aliases" ALTER COLUMN "decision_status" SET DEFAULT 'pending'::"text";

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."normalization_aliases" ALTER COLUMN "source_roles" SET DEFAULT '[]'::"jsonb";

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."normalization_canonicals" ALTER COLUMN "canonical_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_entities_entity_id_seq"'::"regclass");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."normalization_canonicals" ALTER COLUMN "status" SET DEFAULT 'active'::"text";

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."normalization_canonicals" ALTER COLUMN "created_at" SET DEFAULT (CURRENT_TIMESTAMP)::"text";

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."normalization_canonicals" ALTER COLUMN "updated_at" SET DEFAULT (CURRENT_TIMESTAMP)::"text";

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."publisher_merge_analyses" ALTER COLUMN "analysis_id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."publisher_merge_analyses_analysis_id_seq"'::"regclass");

ALTER TABLE ONLY "public"."classification" ALTER COLUMN "id" SET DEFAULT "nextval"('"__CATALOG_SCHEMA__"."catalog_classifications_classification_id_seq"'::"regclass");

ALTER TABLE ONLY "public"."classification" ALTER COLUMN "status" SET DEFAULT 'pending'::"text";

ALTER TABLE ONLY "public"."classification" ALTER COLUMN "created_by" SET DEFAULT 'gemini'::"text";

ALTER TABLE ONLY "public"."classification" ALTER COLUMN "created_at" SET DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_alias_reviews"
    ADD CONSTRAINT "catalog_alias_reviews_entity_type_name_id_key" UNIQUE ("entity_type", "name_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_alias_reviews"
    ADD CONSTRAINT "catalog_alias_reviews_pkey" PRIMARY KEY ("alias_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_aliases"
    ADD CONSTRAINT "catalog_aliases_name_id_entity_id_key" UNIQUE ("name_id", "entity_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_aliases"
    ADD CONSTRAINT "catalog_aliases_pkey" PRIMARY KEY ("alias_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_audiences"
    ADD CONSTRAINT "catalog_audiences_pkey" PRIMARY KEY ("publication_id", "position");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_classification_nodes"
    ADD CONSTRAINT "catalog_classification_nodes_pkey" PRIMARY KEY ("node_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_classifications"
    ADD CONSTRAINT "catalog_classifications_node_id_key" UNIQUE ("node_id") DEFERRABLE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_classifications"
    ADD CONSTRAINT "catalog_classifications_pkey" PRIMARY KEY ("classification_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_collections"
    ADD CONSTRAINT "catalog_collections_pkey" PRIMARY KEY ("collection_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_contributions"
    ADD CONSTRAINT "catalog_contributions_pkey" PRIMARY KEY ("contribution_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_credit_groups"
    ADD CONSTRAINT "catalog_credit_groups_pkey" PRIMARY KEY ("publication_id", "role", "position");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_document_access_modes"
    ADD CONSTRAINT "catalog_document_access_modes_pkey" PRIMARY KEY ("md5", "position");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_documents"
    ADD CONSTRAINT "catalog_documents_pkey" PRIMARY KEY ("md5");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_entities"
    ADD CONSTRAINT "catalog_entities_pkey" PRIMARY KEY ("entity_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_entity_roles"
    ADD CONSTRAINT "catalog_entity_roles_pkey" PRIMARY KEY ("entity_id", "role");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_evidence"
    ADD CONSTRAINT "catalog_evidence_pkey" PRIMARY KEY ("evidence_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_genres"
    ADD CONSTRAINT "catalog_genres_pkey" PRIMARY KEY ("publication_id", "position");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_identifiers"
    ADD CONSTRAINT "catalog_identifiers_pkey" PRIMARY KEY ("publication_id", "kind", "position");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_imports"
    ADD CONSTRAINT "catalog_imports_pkey" PRIMARY KEY ("import_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_imports"
    ADD CONSTRAINT "catalog_imports_source_fingerprint_key" UNIQUE ("source_fingerprint");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_locations"
    ADD CONSTRAINT "catalog_locations_md5_provider_purpose_key" UNIQUE ("md5", "provider", "purpose");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_locations"
    ADD CONSTRAINT "catalog_locations_pkey" PRIMARY KEY ("location_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_names"
    ADD CONSTRAINT "catalog_names_kind_raw_name_key" UNIQUE ("kind", "raw_name");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_names"
    ADD CONSTRAINT "catalog_names_pkey" PRIMARY KEY ("name_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_preview_pages"
    ADD CONSTRAINT "catalog_preview_pages_pkey" PRIMARY KEY ("request_id", "role");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_preview_requests"
    ADD CONSTRAINT "catalog_preview_requests_md5_idempotency_key_key" UNIQUE ("md5", "idempotency_key");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_preview_requests"
    ADD CONSTRAINT "catalog_preview_requests_pkey" PRIMARY KEY ("request_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_proposal_members"
    ADD CONSTRAINT "catalog_proposal_members_pkey" PRIMARY KEY ("member_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_proposals"
    ADD CONSTRAINT "catalog_proposals_pkey" PRIMARY KEY ("proposal_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_protections"
    ADD CONSTRAINT "catalog_protections_pkey" PRIMARY KEY ("record_kind", "record_key", "field");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_publication_languages"
    ADD CONSTRAINT "catalog_publication_languages_pkey" PRIMARY KEY ("publication_id", "position");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_publications"
    ADD CONSTRAINT "catalog_publications_pkey" PRIMARY KEY ("publication_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_reference_authors"
    ADD CONSTRAINT "catalog_reference_authors_pkey" PRIMARY KEY ("publication_id", "position");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_reference_urls"
    ADD CONSTRAINT "catalog_reference_urls_pkey" PRIMARY KEY ("publication_id", "position");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_references"
    ADD CONSTRAINT "catalog_references_pkey" PRIMARY KEY ("publication_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_revisions"
    ADD CONSTRAINT "catalog_revisions_pkey" PRIMARY KEY ("revision_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_separations"
    ADD CONSTRAINT "catalog_separations_pkey" PRIMARY KEY ("left_key", "right_key");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_subjects"
    ADD CONSTRAINT "catalog_subjects_pkey" PRIMARY KEY ("publication_id", "position");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_sufficient_mode_items"
    ADD CONSTRAINT "catalog_sufficient_mode_items_pkey" PRIMARY KEY ("md5", "group_position", "position");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_sufficient_modes"
    ADD CONSTRAINT "catalog_sufficient_modes_pkey" PRIMARY KEY ("md5", "position");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."document_cleanup_queue"
    ADD CONSTRAINT "document_cleanup_queue_pkey" PRIMARY KEY ("cleanup_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_collection_events"
    ADD CONSTRAINT "library_collection_events_pkey" PRIMARY KEY ("event_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_collection_proposal_items"
    ADD CONSTRAINT "library_collection_proposal_items_pkey" PRIMARY KEY ("proposal_id", "md5");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_collection_proposals"
    ADD CONSTRAINT "library_collection_proposals_pkey" PRIMARY KEY ("proposal_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_collection_proposals"
    ADD CONSTRAINT "library_collection_proposals_proposal_key_key" UNIQUE ("proposal_key");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_collection_signatures"
    ADD CONSTRAINT "library_collection_signatures_pkey" PRIMARY KEY ("signature_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_isbn_duplicate_reviews"
    ADD CONSTRAINT "library_isbn_duplicate_reviews_isbn_candidate_hash_key" UNIQUE ("isbn", "candidate_hash");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_isbn_duplicate_reviews"
    ADD CONSTRAINT "library_isbn_duplicate_reviews_pkey" PRIMARY KEY ("review_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_non_pdf_extraction_state"
    ADD CONSTRAINT "library_non_pdf_extraction_state_pkey" PRIMARY KEY ("md5");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_upstream_metadata"
    ADD CONSTRAINT "library_upstream_metadata_pkey" PRIMARY KEY ("md5");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."normalization_events"
    ADD CONSTRAINT "normalization_events_pkey" PRIMARY KEY ("event_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."normalization_suggestions"
    ADD CONSTRAINT "normalization_suggestions_pkey" PRIMARY KEY ("suggestion_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."personality_normalization_checkpoints"
    ADD CONSTRAINT "personality_normalization_checkpoints_pkey" PRIMARY KEY ("raw_name");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."publisher_merge_analyses"
    ADD CONSTRAINT "publisher_merge_analyses_pkey" PRIMARY KEY ("analysis_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."publisher_review_draft"
    ADD CONSTRAINT "publisher_review_draft_pkey" PRIMARY KEY ("singleton");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."publisher_separations"
    ADD CONSTRAINT "publisher_separations_pkey" PRIMARY KEY ("left_key", "right_key");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_contributions"
    ADD CONSTRAINT "uq_catalog_credit_slot" UNIQUE ("publication_id", "role", "position", "nested_position");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_collection_signatures"
    ADD CONSTRAINT "uq_library_collection_signature" UNIQUE ("collection_id", "signature_type", "normalized_value");

ALTER TABLE ONLY "public"."isbn_keep_many"
    ADD CONSTRAINT "isbn_keep_many_pkey" PRIMARY KEY ("isbn_key", "md5");

CREATE INDEX "idx_catalog_alias_reviews_entity" ON "__CATALOG_SCHEMA__"."catalog_alias_reviews" USING "btree" ("entity_id");

CREATE INDEX "idx_catalog_classification_nodes_label_en_trgm" ON "__CATALOG_SCHEMA__"."catalog_classification_nodes" USING "gin" ("label_en" "public"."gin_trgm_ops");

CREATE INDEX "idx_catalog_classification_nodes_label_tt_trgm" ON "__CATALOG_SCHEMA__"."catalog_classification_nodes" USING "gin" ("label_tt" "public"."gin_trgm_ops");

CREATE INDEX "idx_catalog_collections_title_trgm" ON "__CATALOG_SCHEMA__"."catalog_collections" USING "gin" ("title" "public"."gin_trgm_ops");

CREATE INDEX "idx_catalog_contributions_entity" ON "__CATALOG_SCHEMA__"."catalog_contributions" USING "btree" ("entity_id");

CREATE INDEX "idx_catalog_contributions_name_role" ON "__CATALOG_SCHEMA__"."catalog_contributions" USING "btree" ("name_id", "role");

CREATE INDEX "idx_catalog_contributions_publication" ON "__CATALOG_SCHEMA__"."catalog_contributions" USING "btree" ("publication_id");

CREATE INDEX "idx_catalog_documents_publication" ON "__CATALOG_SCHEMA__"."catalog_documents" USING "btree" ("publication_id");

CREATE INDEX "idx_catalog_entities_display_name_trgm" ON "__CATALOG_SCHEMA__"."catalog_entities" USING "gin" ("display_name" "public"."gin_trgm_ops");

CREATE INDEX "idx_catalog_names_raw_name_trgm" ON "__CATALOG_SCHEMA__"."catalog_names" USING "gin" ("raw_name" "public"."gin_trgm_ops");

CREATE INDEX "idx_catalog_preview_queue" ON "__CATALOG_SCHEMA__"."catalog_preview_requests" USING "btree" ("status", "request_id");

CREATE INDEX "idx_catalog_publications_description_trgm" ON "__CATALOG_SCHEMA__"."catalog_publications" USING "gin" ("description" "public"."gin_trgm_ops");

CREATE INDEX "idx_catalog_publications_edition_trgm" ON "__CATALOG_SCHEMA__"."catalog_publications" USING "gin" ("edition" "public"."gin_trgm_ops");

CREATE INDEX "idx_catalog_publications_name_trgm" ON "__CATALOG_SCHEMA__"."catalog_publications" USING "gin" ("name" "public"."gin_trgm_ops");

CREATE INDEX "idx_document_cleanup_status" ON "__CATALOG_SCHEMA__"."document_cleanup_queue" USING "btree" ("status", "cleanup_id");

CREATE INDEX "idx_isbn_duplicate_reviews_status" ON "__CATALOG_SCHEMA__"."library_isbn_duplicate_reviews" USING "btree" ("status", "review_id");

CREATE INDEX "idx_library_collection_proposal_items_md5" ON "__CATALOG_SCHEMA__"."library_collection_proposal_items" USING "btree" ("md5", "proposal_id");

CREATE INDEX "idx_library_collection_proposals_queue" ON "__CATALOG_SCHEMA__"."library_collection_proposals" USING "btree" ("status", "updated_at", "proposal_id");

CREATE INDEX "idx_library_collection_signatures_value_trgm" ON "__CATALOG_SCHEMA__"."library_collection_signatures" USING "gin" ("normalized_value" "public"."gin_trgm_ops");

CREATE INDEX "idx_library_non_pdf_extraction_queue" ON "__CATALOG_SCHEMA__"."library_non_pdf_extraction_state" USING "btree" ("extractor_version", "status", "updated_at", "md5");

CREATE INDEX "idx_norm_events_entity_created" ON "__CATALOG_SCHEMA__"."normalization_events" USING "btree" ("entity_type", "created_at" DESC);

CREATE INDEX "idx_norm_suggestions_entity_status" ON "__CATALOG_SCHEMA__"."normalization_suggestions" USING "btree" ("entity_type", "status");

CREATE INDEX "idx_personality_checkpoint_state" ON "__CATALOG_SCHEMA__"."personality_normalization_checkpoints" USING "btree" ("state", "prompt_version", "schema_version");

CREATE UNIQUE INDEX "uq_catalog_branch" ON "__CATALOG_SCHEMA__"."catalog_classification_nodes" USING "btree" ("parent_id", "label_en") WHERE ("parent_id" IS NOT NULL);

CREATE UNIQUE INDEX "uq_catalog_proposal_entity" ON "__CATALOG_SCHEMA__"."catalog_proposal_members" USING "btree" ("proposal_id", "entity_id") WHERE ("entity_id" IS NOT NULL);

CREATE UNIQUE INDEX "uq_catalog_proposal_name" ON "__CATALOG_SCHEMA__"."catalog_proposal_members" USING "btree" ("proposal_id", "name_id") WHERE ("name_id" IS NOT NULL);

CREATE UNIQUE INDEX "uq_catalog_root" ON "__CATALOG_SCHEMA__"."catalog_classification_nodes" USING "btree" ("ddc", "label_en") WHERE ("parent_id" IS NULL);

CREATE UNIQUE INDEX "uq_document_cleanup_active_md5" ON "__CATALOG_SCHEMA__"."document_cleanup_queue" USING "btree" ("md5") WHERE (("scope" = 'document'::"text") AND ("status" = ANY (ARRAY['planned'::"text", 'running'::"text", 'failed'::"text"])));

CREATE UNIQUE INDEX "uq_document_cleanup_active_resource" ON "__CATALOG_SCHEMA__"."document_cleanup_queue" USING "btree" ("source_resource_id") WHERE (("scope" = 'duplicate_resource'::"text") AND ("source_resource_id" IS NOT NULL) AND ("status" = ANY (ARRAY['planned'::"text", 'running'::"text", 'failed'::"text"])));

CREATE UNIQUE INDEX "uq_document_cleanup_active_source_path" ON "__CATALOG_SCHEMA__"."document_cleanup_queue" USING "btree" ("scope", "source_path") WHERE (("scope" = 'source_resource'::"text") AND ("status" = ANY (ARRAY['planned'::"text", 'running'::"text", 'failed'::"text"])));

CREATE INDEX "ix_isbn_keep_many_isbn_key" ON "public"."isbn_keep_many" USING "btree" ("isbn_key");

CREATE TRIGGER "catalog_alias_review_sync" AFTER INSERT OR DELETE OR UPDATE ON "__CATALOG_SCHEMA__"."catalog_aliases" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_alias_review_sync"();

CREATE TRIGGER "catalog_derive_collection_key" BEFORE INSERT OR UPDATE ON "__CATALOG_SCHEMA__"."catalog_collections" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_derive_keys"();

CREATE TRIGGER "catalog_derive_entity_key" BEFORE INSERT OR UPDATE ON "__CATALOG_SCHEMA__"."catalog_entities" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_derive_keys"();

CREATE TRIGGER "catalog_derive_identifier_key" BEFORE INSERT OR UPDATE ON "__CATALOG_SCHEMA__"."catalog_identifiers" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_derive_keys"();

CREATE TRIGGER "catalog_entity_review_sync" AFTER UPDATE OF "status" ON "__CATALOG_SCHEMA__"."catalog_entities" FOR EACH ROW WHEN (("old"."status" IS DISTINCT FROM "new"."status")) EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_alias_review_sync"();

CREATE TRIGGER "catalog_guard_snapshot" INSTEAD OF DELETE OR UPDATE ON "__CATALOG_SCHEMA__"."library_collection_items" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_command_snapshot_guard"();

CREATE TRIGGER "catalog_guard_snapshot" INSTEAD OF DELETE OR UPDATE ON "__CATALOG_SCHEMA__"."library_collections" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_command_snapshot_guard"();

CREATE TRIGGER "catalog_guard_snapshot" INSTEAD OF DELETE OR UPDATE ON "__CATALOG_SCHEMA__"."normalization_aliases" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_command_snapshot_guard"();

CREATE TRIGGER "catalog_guard_snapshot" INSTEAD OF DELETE OR UPDATE ON "__CATALOG_SCHEMA__"."normalization_canonicals" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_command_snapshot_guard"();

CREATE TRIGGER "catalog_lock_entity_topology" BEFORE INSERT OR DELETE OR UPDATE OF "merged_into_id" ON "__CATALOG_SCHEMA__"."catalog_entities" FOR EACH STATEMENT EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_lock_topology"();

CREATE TRIGGER "catalog_lock_node_topology" BEFORE INSERT OR DELETE OR UPDATE OF "parent_id", "ddc" ON "__CATALOG_SCHEMA__"."catalog_classification_nodes" FOR EACH STATEMENT EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_lock_topology"();

CREATE TRIGGER "catalog_lock_publication_topology" BEFORE INSERT OR DELETE OR UPDATE OF "merged_into_id" ON "__CATALOG_SCHEMA__"."catalog_publications" FOR EACH STATEMENT EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_lock_topology"();

CREATE TRIGGER "catalog_publisher_proposal_write" INSTEAD OF INSERT OR DELETE OR UPDATE ON "__CATALOG_SCHEMA__"."catalog_publisher_proposals" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_publisher_proposal_command"();

CREATE TRIGGER "catalog_review_decision" AFTER UPDATE ON "__CATALOG_SCHEMA__"."catalog_proposals" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_project_review_decision"();

CREATE TRIGGER "catalog_separation_projection" AFTER INSERT OR DELETE ON "__CATALOG_SCHEMA__"."catalog_separations" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_separation_command"();

CREATE TRIGGER "catalog_task_alias" INSTEAD OF INSERT OR DELETE OR UPDATE ON "__CATALOG_SCHEMA__"."normalization_aliases" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_alias_command"();

CREATE TRIGGER "catalog_task_collection" INSTEAD OF INSERT OR DELETE OR UPDATE ON "__CATALOG_SCHEMA__"."library_collections" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_collection_command"();

CREATE TRIGGER "catalog_task_identity" INSTEAD OF INSERT OR DELETE OR UPDATE ON "__CATALOG_SCHEMA__"."normalization_canonicals" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_identity_command"();

CREATE TRIGGER "catalog_task_membership" INSTEAD OF INSERT OR DELETE OR UPDATE ON "__CATALOG_SCHEMA__"."library_collection_items" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_membership_command"();

CREATE TRIGGER "catalog_task_separation" AFTER INSERT OR DELETE ON "__CATALOG_SCHEMA__"."publisher_separations" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_separation_command"();

CREATE TRIGGER "catalog_task_suggestion" AFTER INSERT OR DELETE OR UPDATE ON "__CATALOG_SCHEMA__"."normalization_suggestions" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_review_command"();

CREATE TRIGGER "catalog_validate_entity_topology" BEFORE INSERT OR UPDATE OF "merged_into_id" ON "__CATALOG_SCHEMA__"."catalog_entities" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_validate_topology"('entity_id', 'merged_into_id');

CREATE TRIGGER "catalog_validate_node_topology" BEFORE INSERT OR UPDATE OF "parent_id", "ddc" ON "__CATALOG_SCHEMA__"."catalog_classification_nodes" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_validate_topology"('node_id', 'parent_id');

CREATE TRIGGER "catalog_validate_publication_topology" BEFORE INSERT OR UPDATE OF "merged_into_id" ON "__CATALOG_SCHEMA__"."catalog_publications" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_validate_topology"('publication_id', 'merged_into_id');

CREATE TRIGGER "catalog_validate_reference_url" BEFORE INSERT OR UPDATE ON "__CATALOG_SCHEMA__"."catalog_reference_urls" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_validate_reference_urls"();

CREATE TRIGGER "catalog_validate_reference_url_presence" BEFORE UPDATE OF "urls_present" ON "__CATALOG_SCHEMA__"."catalog_references" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_validate_reference_urls"();

CREATE TRIGGER "catalog_guard_snapshot" INSTEAD OF DELETE OR UPDATE ON "public"."classification" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_command_snapshot_guard"();

CREATE TRIGGER "catalog_guard_snapshot" INSTEAD OF DELETE OR UPDATE ON "public"."document" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_command_snapshot_guard"();

CREATE TRIGGER "catalog_guard_snapshot" INSTEAD OF DELETE OR UPDATE ON "public"."metadata" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_command_snapshot_guard"();

CREATE TRIGGER "catalog_task_classification" INSTEAD OF INSERT OR DELETE OR UPDATE ON "public"."classification" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_classification_command"();

CREATE TRIGGER "catalog_task_document" INSTEAD OF INSERT OR DELETE OR UPDATE ON "public"."document" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_document_command"();

CREATE TRIGGER "catalog_task_metadata" INSTEAD OF INSERT OR DELETE OR UPDATE ON "public"."metadata" FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__"."catalog_task_metadata_command"();

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_alias_reviews"
    ADD CONSTRAINT "catalog_alias_reviews_entity_id_fkey" FOREIGN KEY ("entity_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_entities"("entity_id") ON DELETE RESTRICT;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_alias_reviews"
    ADD CONSTRAINT "catalog_alias_reviews_name_id_fkey" FOREIGN KEY ("name_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_names"("name_id") ON DELETE RESTRICT;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_aliases"
    ADD CONSTRAINT "catalog_aliases_entity_id_fkey" FOREIGN KEY ("entity_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_entities"("entity_id") ON DELETE RESTRICT;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_aliases"
    ADD CONSTRAINT "catalog_aliases_name_id_fkey" FOREIGN KEY ("name_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_names"("name_id") ON DELETE RESTRICT;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_audiences"
    ADD CONSTRAINT "catalog_audiences_publication_id_fkey" FOREIGN KEY ("publication_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_publications"("publication_id") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_classification_nodes"
    ADD CONSTRAINT "catalog_classification_nodes_parent_id_fkey" FOREIGN KEY ("parent_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_classification_nodes"("node_id") ON DELETE RESTRICT;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_classifications"
    ADD CONSTRAINT "catalog_classifications_node_id_fkey" FOREIGN KEY ("node_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_classification_nodes"("node_id") ON DELETE RESTRICT;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_contributions"
    ADD CONSTRAINT "catalog_contributions_entity_id_fkey" FOREIGN KEY ("entity_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_entities"("entity_id") ON DELETE RESTRICT;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_contributions"
    ADD CONSTRAINT "catalog_contributions_name_id_fkey" FOREIGN KEY ("name_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_names"("name_id") ON DELETE RESTRICT;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_credit_groups"
    ADD CONSTRAINT "catalog_credit_groups_publication_id_fkey" FOREIGN KEY ("publication_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_publications"("publication_id") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_document_access_modes"
    ADD CONSTRAINT "catalog_document_access_modes_md5_fkey" FOREIGN KEY ("md5") REFERENCES "__CATALOG_SCHEMA__"."catalog_documents"("md5") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_documents"
    ADD CONSTRAINT "catalog_documents_publication_id_fkey" FOREIGN KEY ("publication_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_publications"("publication_id") ON DELETE RESTRICT;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_entities"
    ADD CONSTRAINT "catalog_entities_merged_into_id_fkey" FOREIGN KEY ("merged_into_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_entities"("entity_id") ON DELETE RESTRICT;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_entity_roles"
    ADD CONSTRAINT "catalog_entity_roles_entity_id_fkey" FOREIGN KEY ("entity_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_entities"("entity_id") ON DELETE RESTRICT;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_genres"
    ADD CONSTRAINT "catalog_genres_publication_id_fkey" FOREIGN KEY ("publication_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_publications"("publication_id") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_identifiers"
    ADD CONSTRAINT "catalog_identifiers_publication_id_fkey" FOREIGN KEY ("publication_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_publications"("publication_id") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_locations"
    ADD CONSTRAINT "catalog_locations_md5_fkey" FOREIGN KEY ("md5") REFERENCES "__CATALOG_SCHEMA__"."catalog_documents"("md5") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_preview_pages"
    ADD CONSTRAINT "catalog_preview_pages_request_id_fkey" FOREIGN KEY ("request_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_preview_requests"("request_id") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_preview_requests"
    ADD CONSTRAINT "catalog_preview_requests_md5_fkey" FOREIGN KEY ("md5") REFERENCES "__CATALOG_SCHEMA__"."catalog_documents"("md5") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_proposal_members"
    ADD CONSTRAINT "catalog_proposal_members_entity_id_fkey" FOREIGN KEY ("entity_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_entities"("entity_id") ON DELETE RESTRICT;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_proposal_members"
    ADD CONSTRAINT "catalog_proposal_members_name_id_fkey" FOREIGN KEY ("name_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_names"("name_id") ON DELETE RESTRICT;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_proposal_members"
    ADD CONSTRAINT "catalog_proposal_members_proposal_id_fkey" FOREIGN KEY ("proposal_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_proposals"("proposal_id") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_proposals"
    ADD CONSTRAINT "catalog_proposals_publication_id_fkey" FOREIGN KEY ("publication_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_publications"("publication_id") ON DELETE RESTRICT;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_publication_languages"
    ADD CONSTRAINT "catalog_publication_languages_publication_id_fkey" FOREIGN KEY ("publication_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_publications"("publication_id") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_publications"
    ADD CONSTRAINT "catalog_publications_classification_id_fkey" FOREIGN KEY ("classification_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_classifications"("classification_id") ON DELETE RESTRICT;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_publications"
    ADD CONSTRAINT "catalog_publications_collection_id_fkey" FOREIGN KEY ("collection_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_collections"("collection_id") ON DELETE RESTRICT;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_publications"
    ADD CONSTRAINT "catalog_publications_merged_into_id_fkey" FOREIGN KEY ("merged_into_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_publications"("publication_id") ON DELETE RESTRICT;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_reference_urls"
    ADD CONSTRAINT "catalog_reference_urls_publication_id_fkey" FOREIGN KEY ("publication_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_references"("publication_id") ON UPDATE CASCADE ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_references"
    ADD CONSTRAINT "catalog_references_publication_id_fkey" FOREIGN KEY ("publication_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_publications"("publication_id") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_subjects"
    ADD CONSTRAINT "catalog_subjects_publication_id_fkey" FOREIGN KEY ("publication_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_publications"("publication_id") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_sufficient_modes"
    ADD CONSTRAINT "catalog_sufficient_modes_md5_fkey" FOREIGN KEY ("md5") REFERENCES "__CATALOG_SCHEMA__"."catalog_documents"("md5") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_contributions"
    ADD CONSTRAINT "fk_catalog_credit_group" FOREIGN KEY ("publication_id", "role", "position") REFERENCES "__CATALOG_SCHEMA__"."catalog_credit_groups"("publication_id", "role", "position") ON UPDATE CASCADE ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_reference_authors"
    ADD CONSTRAINT "fk_catalog_reference_authors_reference" FOREIGN KEY ("publication_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_references"("publication_id") ON UPDATE CASCADE ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."catalog_sufficient_mode_items"
    ADD CONSTRAINT "fk_catalog_sufficient_mode_group" FOREIGN KEY ("md5", "group_position") REFERENCES "__CATALOG_SCHEMA__"."catalog_sufficient_modes"("md5", "position") ON UPDATE CASCADE ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_collection_proposal_items"
    ADD CONSTRAINT "fk_library_collection_proposal_item_proposal" FOREIGN KEY ("proposal_id") REFERENCES "__CATALOG_SCHEMA__"."library_collection_proposals"("proposal_id") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_collection_proposal_items"
    ADD CONSTRAINT "fk_library_collection_proposal_items_document_md5" FOREIGN KEY ("md5") REFERENCES "__CATALOG_SCHEMA__"."catalog_documents"("md5") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_collection_proposals"
    ADD CONSTRAINT "fk_library_collection_proposal_target" FOREIGN KEY ("target_collection_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_collections"("collection_id") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_collection_signatures"
    ADD CONSTRAINT "fk_library_collection_signature_collection" FOREIGN KEY ("collection_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_collections"("collection_id") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."library_non_pdf_extraction_state"
    ADD CONSTRAINT "fk_library_non_pdf_extraction_state_document_md5" FOREIGN KEY ("md5") REFERENCES "__CATALOG_SCHEMA__"."catalog_documents"("md5") ON DELETE CASCADE;

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."normalization_suggestions"
    ADD CONSTRAINT "normalization_suggestions_target_canonical_id_fkey" FOREIGN KEY ("target_canonical_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_entities"("entity_id");

ALTER TABLE ONLY "__CATALOG_SCHEMA__"."personality_normalization_checkpoints"
    ADD CONSTRAINT "personality_normalization_checkpoints_canonical_id_fkey" FOREIGN KEY ("canonical_id") REFERENCES "__CATALOG_SCHEMA__"."catalog_entities"("entity_id");

ALTER TABLE ONLY "public"."isbn_keep_many"
    ADD CONSTRAINT "fk_isbn_keep_many_document_md5" FOREIGN KEY ("md5") REFERENCES "__CATALOG_SCHEMA__"."catalog_documents"("md5") ON DELETE CASCADE;
