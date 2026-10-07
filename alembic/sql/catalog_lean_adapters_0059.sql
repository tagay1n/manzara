-- Frozen lean command contracts. No stored legacy domain projections.

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_array(value JSONB)
RETURNS JSONB LANGUAGE SQL IMMUTABLE AS $fn$
 SELECT CASE WHEN value IS NULL OR value='null'::jsonb THEN '[]'::jsonb
             WHEN jsonb_typeof(value)='array' THEN value ELSE jsonb_build_array(value) END
$fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_path(id BIGINT)
RETURNS JSONB LANGUAGE SQL STABLE
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
 WITH RECURSIVE path AS (
   SELECT n.*,0 AS depth FROM catalog_classification_nodes n WHERE node_id=id
   UNION ALL SELECT n.*,p.depth+1 FROM catalog_classification_nodes n JOIN path p ON n.node_id=p.parent_id
 ) SELECT jsonb_build_object('ddc',max(ddc),'path_en',jsonb_agg(label_en ORDER BY depth DESC),
     'path_tt',CASE WHEN bool_and(label_tt IS NOT NULL) THEN jsonb_agg(label_tt ORDER BY depth DESC) END) FROM path
$fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_schema_org(digest TEXT)
RETURNS JSONB LANGUAGE plpgsql STABLE
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
#variable_conflict use_column
<<catalog_array>>
DECLARE d catalog_documents; p catalog_publications; result JSONB; value JSONB; credit RECORD;
        taxonomy JSONB; node BIGINT;
BEGIN
 SELECT * INTO d FROM catalog_documents WHERE md5=digest;
 SELECT * INTO p FROM catalog_publications WHERE publication_id=d.publication_id;
 IF p.publication_id IS NULL THEN RETURN NULL; END IF;
 result=jsonb_strip_nulls(jsonb_build_object('@context','https://schema.org','@type',p.work_type,
   'name',p.name,'description',p.description,'bookEdition',p.edition,'datePublished',p.date_published,
   'numberOfPages',p.page_count));
 IF cardinality(p.languages)>0 THEN result=result||jsonb_build_object('inLanguage',array_to_string(p.languages,',')); END IF;
 FOR credit IN
   SELECT c.role,c.position,c.role_name,jsonb_agg(jsonb_build_object('@type',initcap(n.kind),
      'name',coalesce(e.display_name,n.raw_name)) ORDER BY c.nested_position) AS people
   FROM catalog_contributions c JOIN catalog_names n USING(name_id) LEFT JOIN catalog_entities e USING(entity_id)
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
 FROM catalog_sufficient_modes WHERE md5=digest;
 IF value IS NOT NULL THEN result=result||jsonb_build_object('accessModeSufficient',value); END IF;
 SELECT jsonb_strip_nulls(jsonb_build_object('@type',work_type,'name',name,'inLanguage',language,'url',urls)) INTO value
 FROM catalog_references WHERE publication_id=p.publication_id;
 IF value IS NOT NULL THEN
   SELECT jsonb_agg(jsonb_build_object('@type',kind,'name',name) ORDER BY position) INTO taxonomy
   FROM catalog_reference_authors WHERE publication_id=p.publication_id;
   IF taxonomy IS NOT NULL THEN value=value||jsonb_build_object('author',taxonomy); END IF;
   result=result||jsonb_build_object('isBasedOn',value);
 END IF;
 RETURN result;
END $fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_task_patch(kind TEXT, key TEXT, changes JSONB, digest TEXT DEFAULT NULL)
RETURNS VOID LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
#variable_conflict use_column
<<catalog_task_patch>>
DECLARE relation TEXT; pk TEXT; before JSONB; after JSONB; allowed JSONB='{}'; blocked JSONB='{}';
        field TEXT; value JSONB; assignments TEXT; actor TEXT; proposal BIGINT;
BEGIN
 actor=coalesce(nullif(current_setting('manzara.catalog_actor',true),''),'task');
 CASE kind WHEN 'publication' THEN relation='catalog_publications';pk='publication_id';
   WHEN 'document' THEN relation='catalog_documents';pk='md5';
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
END $fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_task_metadata(digest TEXT, source JSONB)
RETURNS VOID LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
#variable_conflict use_column
<<catalog_task_metadata>>
DECLARE pub BIGINT; old JSONB; normalized JSONB; scalar JSONB='{}'; field TEXT; column_name TEXT;
        value JSONB; protected TEXT[]; doc_protected TEXT[]; blocked JSONB='{}';
        actor TEXT; role TEXT; item JSONB; person JSONB; pos INTEGER; nested INTEGER;
        raw TEXT; kind TEXT; name_key BIGINT; current_credit catalog_contributions; protected_credits BOOLEAN;
BEGIN
 IF source IS NULL THEN RETURN; END IF;
 IF jsonb_typeof(source)<>'object' OR source->>'@context' NOT IN ('https://schema.org') THEN
   RAISE EXCEPTION 'unsupported metadata envelope'; END IF;
 IF EXISTS(SELECT 1 FROM jsonb_object_keys(source) f WHERE f NOT IN
   ('@context','@type','name','description','datePublished','numberOfPages','bookEdition','inLanguage',
    'author','editor','translator','illustrator','publisher','contributor','isbn','genre','about','audience','accessMode','accessModeSufficient','isBasedOn')) THEN
   RAISE EXCEPTION 'unsupported metadata fields'; END IF;
 SELECT publication_id INTO pub FROM catalog_documents WHERE md5=digest FOR UPDATE;
 PERFORM 1 FROM catalog_publications WHERE publication_id=pub FOR UPDATE;
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
       SELECT c.* INTO current_credit FROM catalog_contributions c JOIN catalog_names n USING(name_id) LEFT JOIN catalog_entities e USING(entity_id)
         WHERE c.publication_id=pub AND c.role=catalog_task_metadata.role AND c.position=pos AND c.nested_position=nested AND n.kind=catalog_task_metadata.kind
         AND c.role_name IS NOT DISTINCT FROM (CASE WHEN item->>'@type'='Role' THEN item->>'roleName' END)
         AND (n.raw_name=raw OR e.display_name=raw);
       IF current_credit.contribution_id IS NOT NULL THEN SELECT raw_name INTO raw FROM catalog_names WHERE name_id=current_credit.name_id; END IF;
       normalized=normalized||jsonb_build_array(jsonb_build_object('role',role,'role_name',CASE WHEN item->>'@type'='Role' THEN item->>'roleName' END,
         'position',pos,'nested_position',nested,'raw_name',raw,'kind',kind));
       IF NOT protected_credits AND current_credit.contribution_id IS NULL THEN
         INSERT INTO catalog_names(kind,raw_name) VALUES(kind,raw) ON CONFLICT(kind,raw_name) DO UPDATE SET raw_name=EXCLUDED.raw_name RETURNING name_id INTO name_key;
         INSERT INTO catalog_contributions(publication_id,name_id,role,role_name,position,nested_position)
           VALUES(pub,name_key,role,CASE WHEN item->>'@type'='Role' THEN item->>'roleName' END,pos,nested);
       END IF;
     END LOOP;
   END LOOP;
 END LOOP;
 SELECT coalesce(jsonb_agg(x ORDER BY x->>'role',(x->>'position')::integer,(x->>'nested_position')::integer),'[]') INTO normalized FROM jsonb_array_elements(normalized) x;
 IF protected_credits THEN
   IF normalized IS DISTINCT FROM (SELECT coalesce(jsonb_agg(jsonb_build_object('role',c.role,'role_name',c.role_name,'position',c.position,
      'nested_position',c.nested_position,'raw_name',n.raw_name,'kind',n.kind) ORDER BY c.role,c.position,c.nested_position),'[]')
      FROM catalog_contributions c JOIN catalog_names n USING(name_id) WHERE publication_id=pub) THEN blocked=blocked||jsonb_build_object('credits',normalized); END IF;
 ELSE
   DELETE FROM catalog_contributions c WHERE publication_id=pub AND NOT EXISTS(
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
       WHERE NOT EXISTS(SELECT 1 FROM catalog_publications WHERE publication_id=pub AND classification_id IS NOT NULL)
         OR lower(coalesce(v->'inDefinedTermSet'->>'name','')) NOT IN ('ddc','categorypath');
   END IF;
 END LOOP;
 IF catalog_array(source->'accessMode') IS DISTINCT FROM catalog_array(old->'accessMode') THEN
   IF 'access_modes'=ANY(doc_protected) THEN blocked=blocked||jsonb_build_object('access_modes',catalog_array(source->'accessMode'));
   ELSE PERFORM catalog_task_patch('document',digest,jsonb_build_object('access_modes',catalog_array(source->'accessMode')),digest); END IF;
 END IF;
 IF catalog_array(source->'accessModeSufficient') IS DISTINCT FROM catalog_array(old->'accessModeSufficient') THEN
   IF 'sufficient_modes'=ANY(doc_protected) THEN blocked=blocked||jsonb_build_object('sufficient_modes',(SELECT coalesce(jsonb_agg(v->'itemListElement'),'[]') FROM jsonb_array_elements(catalog_array(source->'accessModeSufficient')) v));
   ELSE DELETE FROM catalog_sufficient_modes WHERE md5=digest;
     INSERT INTO catalog_sufficient_modes(md5,position,modes) SELECT digest,(ordinality-1)::integer,
       ARRAY(SELECT jsonb_array_elements_text(v->'itemListElement')) FROM jsonb_array_elements(catalog_array(source->'accessModeSufficient')) WITH ORDINALITY x(v,ordinality);
   END IF;
 END IF;
 IF source->'isBasedOn' IS DISTINCT FROM old->'isBasedOn' THEN
   value=source->'isBasedOn';
   IF 'based_on'=ANY(protected) THEN blocked=blocked||jsonb_build_object('based_on',value);
   ELSE DELETE FROM catalog_reference_authors WHERE publication_id=pub; DELETE FROM catalog_references WHERE publication_id=pub;
     IF value IS NOT NULL AND value<>'null' THEN
       IF EXISTS(SELECT 1 FROM jsonb_object_keys(value) f WHERE f NOT IN ('@type','name','inLanguage','url','author')) THEN RAISE EXCEPTION 'unsupported source-work reference'; END IF;
       INSERT INTO catalog_references(publication_id,work_type,name,language,urls) VALUES(pub,value->>'@type',value->>'name',value->>'inLanguage',CASE WHEN value ? 'url' THEN ARRAY(SELECT jsonb_array_elements_text(catalog_array(value->'url'))) END);
       INSERT INTO catalog_reference_authors(publication_id,position,kind,name) SELECT pub,(ordinality-1)::integer,v->>'@type',v->>'name'
         FROM jsonb_array_elements(catalog_array(value->'author')) WITH ORDINALITY x(v,ordinality);
     END IF;
   END IF;
 END IF;
 IF blocked<>'{}' THEN INSERT INTO catalog_proposals(kind,publication_id,evidence,field_changes)
   VALUES('metadata',pub,jsonb_build_object('md5',digest,'actor',actor),blocked); END IF;
 INSERT INTO catalog_evidence(md5,record_kind,record_key,source,payload) VALUES(digest,'publication',pub::text,'task.metadata',source);
 -- Relations affect the same optimistic revision as columns.
 UPDATE catalog_publications SET revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE publication_id=pub;
END $fn$;


CREATE VIEW "__DATASET_SCHEMA__".document AS
 SELECT d.md5,d.mime_type,
 (SELECT source_path FROM "__CATALOG_SCHEMA__".catalog_locations WHERE md5=d.md5 AND provider='yandex' AND purpose='source') AS ya_path,
 CASE WHEN NOT d.restricted THEN (SELECT public_url FROM "__CATALOG_SCHEMA__".catalog_locations WHERE md5=d.md5 AND provider='yandex' AND purpose='source') END AS ya_public_url,
 CASE WHEN NOT d.restricted THEN (SELECT public_key FROM "__CATALOG_SCHEMA__".catalog_locations WHERE md5=d.md5 AND provider='yandex' AND purpose='source') END AS ya_public_key,
 (SELECT resource_id FROM "__CATALOG_SCHEMA__".catalog_locations WHERE md5=d.md5 AND provider='yandex' AND purpose='source') AS ya_resource_id,
 nullif(array_to_string(p.languages,','),'') AS language,d.content_extraction_method,d.meta_extraction_method,
 d.complete AS "full",d.restricted AS sharing_restricted,
 (SELECT locator FROM "__CATALOG_SCHEMA__".catalog_locations WHERE md5=d.md5 AND provider='s3' AND purpose='primary') AS document_url,
 (SELECT locator FROM "__CATALOG_SCHEMA__".catalog_locations WHERE md5=d.md5 AND provider='s3' AND purpose='content') AS content_url,
 (SELECT size FROM "__CATALOG_SCHEMA__".catalog_locations WHERE md5=d.md5 AND provider='s3' AND purpose='primary') AS primary_storage_size,
 (SELECT etag FROM "__CATALOG_SCHEMA__".catalog_locations WHERE md5=d.md5 AND provider='s3' AND purpose='primary') AS primary_storage_etag,
 (SELECT verified_at FROM "__CATALOG_SCHEMA__".catalog_locations WHERE md5=d.md5 AND provider='s3' AND purpose='primary') AS primary_storage_verified_at
 FROM "__CATALOG_SCHEMA__".catalog_documents d JOIN "__CATALOG_SCHEMA__".catalog_publications p USING(publication_id);
CREATE VIEW "__DATASET_SCHEMA__".metadata AS
 SELECT d.md5,CASE WHEN p.metadata_present THEN "__CATALOG_SCHEMA__".catalog_schema_org(d.md5)::json END AS schema_org,
 CASE p.inclusion WHEN 'included' THEN true WHEN 'excluded' THEN false END AS lib,
 p.evaluation_method AS lib_eval_method,p.classification_id
 FROM "__CATALOG_SCHEMA__".catalog_documents d JOIN "__CATALOG_SCHEMA__".catalog_publications p USING(publication_id) WHERE p.has_metadata;
CREATE VIEW "__DATASET_SCHEMA__".classification AS
 SELECT c.classification_id AS id,path.data->>'ddc' AS ddc,(path.data->'path_en')::json AS path_en,
 (SELECT lower(string_agg(value,'|' ORDER BY ordinality)) FROM jsonb_array_elements_text(path.data->'path_en') WITH ORDINALITY) AS path_en_key,
 nullif(path.data->'path_tt','null'::jsonb)::json AS path_tt,c.status,c.created_by,coalesce(c.created_at,CURRENT_TIMESTAMP::timestamp) AS created_at
 FROM "__CATALOG_SCHEMA__".catalog_classifications c CROSS JOIN LATERAL (SELECT "__CATALOG_SCHEMA__".catalog_path(c.node_id) AS data) path;
CREATE VIEW "__CATALOG_SCHEMA__".normalization_canonicals AS
 SELECT e.entity_id AS canonical_id,CASE WHEN e.kind<>'person' OR EXISTS(SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_entity_roles r WHERE r.entity_id=e.entity_id AND r.role='publisher') OR EXISTS(SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_contributions c WHERE c.entity_id=e.entity_id AND c.role='publisher') THEN 'publisher' ELSE 'personality' END AS entity_type,
 e.display_name,coalesce(e.normalized_name,lower(e.display_name)) AS normalized_name,e.status,e.merged_into_id,e.notes,
 coalesce(e.created_at,e.updated_at::text) AS created_at,
 CASE WHEN e.revision=1 AND e.source_updated_at IS NOT NULL THEN e.source_updated_at ELSE e.updated_at::text END AS updated_at,
 e.surname_full,e.surname_initials,e.name_full,e.name_initials,e.father_name_full,e.father_name_initials,e.title,e.sex,e.identity_key
 FROM "__CATALOG_SCHEMA__".catalog_entities e;
CREATE VIEW "__CATALOG_SCHEMA__".normalization_aliases AS
 SELECT r.alias_id,r.entity_type,n.raw_name,r.normalized_name,r.script_label,r.docs_count,r.mentions_count,r.marker_count,
 r.decision_status,r.entity_id AS canonical_id,r.confidence,r.source,r.reason,r.created_at,r.updated_at,
 r.surname_full,r.surname_initials,r.name_full,r.name_initials,r.father_name_full,r.father_name_initials,r.title,r.sex,
 r.source_roles,r.successful_model,r.prompt_version,r.schema_version
 FROM "__CATALOG_SCHEMA__".catalog_alias_reviews r JOIN "__CATALOG_SCHEMA__".catalog_names n USING(name_id);
CREATE VIEW "__CATALOG_SCHEMA__".library_collections AS
 SELECT c.collection_id,c.title,coalesce(c.normalized_title,lower(c.title)) AS normalized_title,c.include_in_library::integer::bigint AS include_in_library,
 c.metadata_template_json,c.notes,c.applied_at,coalesce(c.created_at,c.updated_at::text) AS created_at,
 CASE WHEN c.revision=1 AND c.source_updated_at IS NOT NULL THEN c.source_updated_at ELSE c.updated_at::text END AS updated_at
 FROM "__CATALOG_SCHEMA__".catalog_collections c;
CREATE VIEW "__CATALOG_SCHEMA__".library_collection_items AS
 SELECT p.collection_id,d.md5,p.collection_item_title AS item_title,coalesce(p.collection_created_at,p.updated_at::text) AS created_at,
 coalesce(p.collection_updated_at,p.updated_at::text) AS updated_at
 FROM "__CATALOG_SCHEMA__".catalog_documents d JOIN "__CATALOG_SCHEMA__".catalog_publications p USING(publication_id) WHERE p.collection_id IS NOT NULL;
ALTER VIEW "__DATASET_SCHEMA__".classification ALTER COLUMN id SET DEFAULT nextval('"__CATALOG_SCHEMA__".catalog_classifications_classification_id_seq');
ALTER VIEW "__DATASET_SCHEMA__".classification ALTER COLUMN status SET DEFAULT 'pending';
ALTER VIEW "__DATASET_SCHEMA__".classification ALTER COLUMN created_by SET DEFAULT 'gemini';
ALTER VIEW "__DATASET_SCHEMA__".classification ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP;
ALTER VIEW "__CATALOG_SCHEMA__".normalization_canonicals ALTER COLUMN canonical_id SET DEFAULT nextval('"__CATALOG_SCHEMA__".catalog_entities_entity_id_seq');
ALTER VIEW "__CATALOG_SCHEMA__".normalization_canonicals ALTER COLUMN status SET DEFAULT 'active';
ALTER VIEW "__CATALOG_SCHEMA__".normalization_canonicals ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP::text;
ALTER VIEW "__CATALOG_SCHEMA__".normalization_canonicals ALTER COLUMN updated_at SET DEFAULT CURRENT_TIMESTAMP::text;
ALTER VIEW "__CATALOG_SCHEMA__".normalization_aliases ALTER COLUMN alias_id SET DEFAULT nextval('"__CATALOG_SCHEMA__".catalog_alias_reviews_alias_id_seq');
ALTER VIEW "__CATALOG_SCHEMA__".normalization_aliases ALTER COLUMN decision_status SET DEFAULT 'pending';
ALTER VIEW "__CATALOG_SCHEMA__".normalization_aliases ALTER COLUMN docs_count SET DEFAULT 0;
ALTER VIEW "__CATALOG_SCHEMA__".normalization_aliases ALTER COLUMN mentions_count SET DEFAULT 0;
ALTER VIEW "__CATALOG_SCHEMA__".normalization_aliases ALTER COLUMN marker_count SET DEFAULT 0;
ALTER VIEW "__CATALOG_SCHEMA__".normalization_aliases ALTER COLUMN source_roles SET DEFAULT '[]'::jsonb;
ALTER VIEW "__CATALOG_SCHEMA__".library_collections ALTER COLUMN collection_id SET DEFAULT nextval('"__CATALOG_SCHEMA__".catalog_collections_collection_id_seq');
ALTER VIEW "__CATALOG_SCHEMA__".library_collections ALTER COLUMN include_in_library SET DEFAULT 1;
ALTER VIEW "__CATALOG_SCHEMA__".library_collections ALTER COLUMN metadata_template_json SET DEFAULT '{}';


CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_task_document_command()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
#variable_conflict use_column
<<catalog_task_document_command>>
DECLARE pub BIGINT; d catalog_documents; y catalog_locations; s catalog_locations; t catalog_locations;
        fields JSONB; before JSONB; old_fields JSONB;
BEGIN
 IF current_setting('manzara.catalog_projection',true)='on' THEN RETURN coalesce(NEW,OLD); END IF;
 IF TG_OP='DELETE' THEN
   DELETE FROM catalog_documents WHERE md5=OLD.md5; RETURN OLD;
 END IF;
 IF NEW.md5 !~ '^[0-9a-f]{32}$' THEN RAISE EXCEPTION 'invalid document md5'; END IF;
 SELECT * INTO d FROM catalog_documents WHERE md5=NEW.md5 FOR UPDATE;
 IF d.md5 IS NULL THEN
   INSERT INTO catalog_publications(has_metadata,metadata_present) VALUES(false,false) RETURNING publication_id INTO pub;
   INSERT INTO catalog_documents(md5,publication_id,mime_type,complete,restricted)
     VALUES(NEW.md5,pub,NEW.mime_type,coalesce(NEW."full",false),coalesce(NEW.sharing_restricted,true));
 ELSE pub=d.publication_id; END IF;
 PERFORM catalog_task_patch('document',NEW.md5,jsonb_build_object('mime_type',NEW.mime_type,'complete',coalesce(NEW."full",false),
   'restricted',coalesce(NEW.sharing_restricted,true),'content_extraction_method',NEW.content_extraction_method,
   'meta_extraction_method',NEW.meta_extraction_method),NEW.md5);
 IF NEW.language IS NOT NULL AND (TG_OP='INSERT' OR NEW.language IS DISTINCT FROM OLD.language) THEN
   PERFORM catalog_task_patch('publication',pub::text,jsonb_build_object('languages',to_jsonb(ARRAY(SELECT btrim(x)
     FROM unnest(string_to_array(NEW.language,',')) x WHERE btrim(x)<>''))),NEW.md5);
 END IF;
 SELECT * INTO d FROM catalog_documents WHERE md5=NEW.md5;
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
 SELECT nullif(array_to_string(languages,','),'') INTO NEW.language FROM catalog_publications WHERE publication_id=pub;
 IF d.restricted THEN NEW.ya_public_url=NULL;NEW.ya_public_key=NULL; END IF;
 RETURN NEW;
END $fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_task_metadata_command()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
#variable_conflict use_column
<<catalog_task_metadata_command>>
DECLARE pub catalog_publications; changes JSONB='{}';
BEGIN
 IF current_setting('manzara.catalog_projection',true)='on' THEN RETURN coalesce(NEW,OLD); END IF;
 IF TG_OP='DELETE' THEN
   IF NOT EXISTS(SELECT 1 FROM catalog_documents WHERE md5=OLD.md5) THEN RETURN OLD; END IF;
   RAISE EXCEPTION 'metadata deletion requires explicit catalog review';
 END IF;
 SELECT p.* INTO pub FROM catalog_publications p JOIN catalog_documents d USING(publication_id) WHERE d.md5=NEW.md5 FOR UPDATE OF p;
 IF pub.publication_id IS NULL THEN RAISE EXCEPTION 'document missing'; END IF;
 UPDATE catalog_publications SET has_metadata=true,metadata_present=NEW.schema_org IS NOT NULL WHERE publication_id=pub.publication_id;
 IF TG_OP='INSERT' OR NEW.schema_org::jsonb IS DISTINCT FROM OLD.schema_org::jsonb THEN
   PERFORM catalog_task_metadata(NEW.md5,NEW.schema_org::jsonb);
 END IF;
 IF TG_OP='INSERT' OR NEW.lib IS DISTINCT FROM OLD.lib THEN changes=changes||jsonb_build_object('inclusion',CASE NEW.lib WHEN TRUE THEN 'included' WHEN FALSE THEN 'excluded' ELSE 'pending' END); END IF;
 IF TG_OP='INSERT' OR NEW.lib_eval_method IS DISTINCT FROM OLD.lib_eval_method THEN changes=changes||jsonb_build_object('evaluation_method',NEW.lib_eval_method); END IF;
 IF TG_OP='INSERT' OR NEW.classification_id IS DISTINCT FROM OLD.classification_id THEN changes=changes||jsonb_build_object('classification_id',NEW.classification_id); END IF;
 PERFORM catalog_task_patch('publication',pub.publication_id::text,changes,NEW.md5);
 SELECT * INTO pub FROM catalog_publications WHERE publication_id=pub.publication_id;
 NEW.schema_org=catalog_schema_org(NEW.md5);NEW.lib=CASE pub.inclusion WHEN 'included' THEN TRUE WHEN 'excluded' THEN FALSE END;
 NEW.lib_eval_method=pub.evaluation_method;NEW.classification_id=pub.classification_id;
 RETURN NEW;
END $fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_task_classification_command()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
#variable_conflict use_column
<<catalog_task_classification_command>>
DECLARE parent BIGINT=NULL; node BIGINT; label TEXT; translated TEXT; pos INTEGER; old_node BIGINT; actor TEXT;
BEGIN
 IF current_setting('manzara.catalog_projection',true)='on' THEN RETURN coalesce(NEW,OLD); END IF;
 IF TG_OP='DELETE' THEN
   UPDATE catalog_publications SET classification_id=NULL,revision=revision+1 WHERE classification_id=OLD.id; DELETE FROM catalog_classifications WHERE classification_id=OLD.id;RETURN OLD;
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
   UPDATE catalog_publications SET revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE classification_id=NEW.id;
 END IF;
 PERFORM setval(pg_get_serial_sequence('catalog_classifications','classification_id'),greatest(NEW.id,(SELECT coalesce(max(classification_id),1) FROM catalog_classifications)),true);
 RETURN NEW;
END $fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_task_collection_command()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
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
 NEW.normalized_title=CASE WHEN NEW.title=c.title THEN coalesce(NEW.normalized_title,lower(c.title)) ELSE coalesce(c.normalized_title,lower(c.title)) END;NEW.title=c.title;NEW.notes=c.notes;NEW.include_in_library=c.include_in_library::integer;
 UPDATE catalog_collections SET normalized_title=NEW.normalized_title,metadata_template_json=NEW.metadata_template_json,
 applied_at=NEW.applied_at,created_at=coalesce(created_at,NEW.created_at),source_updated_at=NEW.updated_at WHERE collection_id=NEW.collection_id;
 PERFORM setval(pg_get_serial_sequence('catalog_collections','collection_id'),greatest(NEW.collection_id,(SELECT coalesce(max(collection_id),1) FROM catalog_collections)),true);
 RETURN NEW;
END $fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_task_membership_command()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
#variable_conflict use_column
<<catalog_task_membership_command>>
DECLARE pub BIGINT; membership BIGINT;
BEGIN
 IF current_setting('manzara.catalog_projection',true)='on' THEN RETURN coalesce(NEW,OLD); END IF;
 SELECT publication_id INTO pub FROM catalog_documents WHERE md5=CASE WHEN TG_OP='DELETE' THEN OLD.md5 ELSE NEW.md5 END;
 IF pub IS NULL THEN RETURN coalesce(NEW,OLD); END IF;
 IF TG_OP='INSERT' AND EXISTS(SELECT 1 FROM library_collection_items WHERE md5=NEW.md5) THEN RETURN NEW; END IF;
 PERFORM catalog_task_patch('publication',pub::text,jsonb_build_object('collection_id',CASE WHEN TG_OP='DELETE' THEN NULL ELSE NEW.collection_id END),CASE WHEN TG_OP='DELETE' THEN OLD.md5 ELSE NEW.md5 END);
 UPDATE catalog_publications SET collection_item_title=CASE WHEN TG_OP='DELETE' THEN NULL ELSE NEW.item_title END,
 collection_created_at=CASE WHEN TG_OP='DELETE' THEN NULL ELSE NEW.created_at END,
 collection_updated_at=CASE WHEN TG_OP='DELETE' THEN NULL ELSE NEW.updated_at END WHERE publication_id=pub;
 IF TG_OP<>'DELETE' THEN
   SELECT collection_id INTO membership FROM catalog_publications WHERE publication_id=pub;
   IF membership IS NULL THEN RAISE EXCEPTION 'collection assignment awaits review'; END IF;
   NEW.collection_id=membership;
 END IF;
 RETURN coalesce(NEW,OLD);
END $fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_task_identity_command()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
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
   UPDATE catalog_publications SET revision=revision+1,updated_at=CURRENT_TIMESTAMP
     WHERE publication_id IN (SELECT publication_id FROM catalog_contributions WHERE entity_id=e.entity_id);
 END IF;
 IF e.status='merged' AND e.merged_into_id IS NOT NULL THEN
   IF e.merged_into_id=e.entity_id OR NOT EXISTS(SELECT 1 FROM catalog_entities WHERE entity_id=e.merged_into_id AND status='active' AND kind=e.kind) THEN
     RAISE EXCEPTION 'invalid identity merge target'; END IF;
   INSERT INTO catalog_aliases(name_id,entity_id,approval) SELECT name_id,e.merged_into_id,approval FROM catalog_aliases WHERE entity_id=e.entity_id ON CONFLICT(name_id,entity_id) DO NOTHING;
   INSERT INTO catalog_entity_roles(entity_id,role) SELECT e.merged_into_id,role FROM catalog_entity_roles WHERE entity_id=e.entity_id ON CONFLICT DO NOTHING;
   -- Migrate reviewed identities, never every occurrence of a spelling.
   FOR p IN SELECT DISTINCT publication_id FROM catalog_contributions WHERE entity_id=e.entity_id LOOP
     UPDATE catalog_publications SET revision=revision+1 WHERE publication_id=p;
   END LOOP;
   UPDATE catalog_contributions SET entity_id=e.merged_into_id,revision=revision+1 WHERE entity_id=e.entity_id;
 END IF;
 NEW.display_name=e.display_name;NEW.normalized_name=lower(e.display_name);NEW.status=e.status;NEW.merged_into_id=e.merged_into_id;
 NEW.notes=e.notes;NEW.surname_full=e.surname_full;NEW.surname_initials=e.surname_initials;NEW.name_full=e.name_full;
 NEW.name_initials=e.name_initials;NEW.father_name_full=e.father_name_full;NEW.father_name_initials=e.father_name_initials;
 NEW.title=e.title;NEW.sex=e.sex;NEW.identity_key=e.identity_key;
 UPDATE catalog_entities SET normalized_name=NEW.normalized_name,created_at=coalesce(created_at,NEW.created_at),source_updated_at=NEW.updated_at WHERE entity_id=NEW.canonical_id;
 PERFORM setval(pg_get_serial_sequence('catalog_entities','entity_id'),greatest(NEW.canonical_id,(SELECT coalesce(max(entity_id),1) FROM catalog_entities)),true);
 RETURN NEW;
END $fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_task_alias_command()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
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
END $fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_task_preview_command()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
#variable_conflict use_column
DECLARE id BIGINT; role TEXT; suffix TEXT; number INTEGER;
BEGIN
 IF NEW.status<>'ready' OR EXISTS(SELECT 1 FROM catalog_documents WHERE md5=NEW.md5 AND restricted) THEN RETURN NEW; END IF;
 INSERT INTO catalog_preview_requests(md5,idempotency_key,recipe,status,private,actor,source_page_count)
   VALUES(NEW.md5,'task:'||NEW.recipe_version||':'||NEW.attempt_count,NEW.recipe_version,'ready',false,'task.preview',NEW.source_page_count)
   ON CONFLICT(md5,idempotency_key) DO NOTHING RETURNING request_id INTO id;
 IF id IS NULL THEN RETURN NEW; END IF;
 FOREACH role IN ARRAY ARRAY['first','second','last'] LOOP
   number=(to_jsonb(NEW)->>(role||'_preview_page'))::integer;
   suffix=CASE role WHEN 'first' THEN '1' WHEN 'second' THEN '2' ELSE 'l' END;
   IF number IS NOT NULL THEN INSERT INTO catalog_preview_pages(request_id,role,page_number,small_key,large_key)
     VALUES(id,role,number,NEW.md5||'/'||suffix||'s.webp',NEW.md5||'/'||suffix||'l.webp'); END IF;
 END LOOP;
 RETURN NEW;
END $fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_publisher_name_kind(raw TEXT)
RETURNS TEXT LANGUAGE plpgsql STABLE
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
DECLARE kinds TEXT[];
BEGIN
 SELECT array_agg(DISTINCT n.kind) INTO kinds FROM catalog_names n
   JOIN catalog_contributions c USING(name_id) WHERE n.raw_name=raw AND c.role='publisher';
 IF coalesce(cardinality(kinds),0)=0 THEN
   SELECT array_agg(DISTINCT n.kind) INTO kinds FROM catalog_names n
     JOIN catalog_aliases a USING(name_id) JOIN catalog_entity_roles r USING(entity_id)
     WHERE n.raw_name=raw AND r.role='publisher';
 END IF;
 IF cardinality(kinds)>1 THEN RAISE EXCEPTION 'ambiguous publisher name type; review in catalog'; END IF;
 RETURN coalesce(kinds[1],'organization');
END $fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_review_member(proposal BIGINT, key TEXT, kind TEXT)
RETURNS VOID LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
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
END $fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_internal_review_key(key TEXT)
RETURNS TEXT LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
DECLARE id BIGINT; name_kind TEXT;
BEGIN
 IF key LIKE 'canonical:%' THEN RETURN 'entity:'||substring(key FROM 11); END IF;
 IF key NOT LIKE 'raw:%' OR length(key)<=4 THEN RAISE EXCEPTION 'unsupported separation key'; END IF;
 name_kind=catalog_publisher_name_kind(substring(key FROM 5));
 INSERT INTO catalog_names(kind,raw_name) VALUES(name_kind,substring(key FROM 5))
   ON CONFLICT(kind,raw_name) DO UPDATE SET raw_name=EXCLUDED.raw_name RETURNING name_id INTO id;
 RETURN 'name:'||id;
END $fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_external_review_key(key TEXT)
RETURNS TEXT LANGUAGE SQL STABLE
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
 SELECT CASE WHEN key LIKE 'entity:%' THEN 'canonical:'||substring(key FROM 8)
   ELSE (SELECT 'raw:'||raw_name FROM catalog_names WHERE name_id=substring(key FROM 6)::bigint) END
$fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_task_review_command()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
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
END $fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_project_review_decision()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
DECLARE previous TEXT; id BIGINT;
BEGIN
 IF NEW.status=OLD.status OR current_setting('manzara.catalog_projection',true)='on' THEN RETURN NEW; END IF;
 previous=coalesce(current_setting('manzara.catalog_projection',true),'');
 PERFORM set_config('manzara.catalog_projection','on',true);
 id=(NEW.evidence->>'legacy_id')::bigint;
 IF NEW.evidence->>'source'='legacy.normalization_suggestions' THEN
   UPDATE normalization_suggestions SET status=CASE NEW.status WHEN 'applied' THEN 'accepted' WHEN 'separate' THEN 'dismissed' WHEN 'rejected' THEN 'dismissed' ELSE 'open' END,updated_at=CURRENT_TIMESTAMP::text WHERE suggestion_id=id;
 ELSIF NEW.evidence->>'source'='legacy.publisher_merge_proposals' THEN
   UPDATE publisher_merge_proposals SET status=CASE NEW.status WHEN 'applied' THEN 'applied' WHEN 'separate' THEN 'separate' ELSE 'skipped' END,updated_at=CURRENT_TIMESTAMP::text WHERE proposal_id=id;
 END IF;
 PERFORM set_config('manzara.catalog_projection',previous,true);
 RETURN NEW;
END $fn$;

CREATE OR REPLACE FUNCTION "__CATALOG_SCHEMA__".catalog_separation_command()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
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
END $fn$;

CREATE TRIGGER catalog_task_document INSTEAD OF INSERT OR UPDATE OR DELETE ON "__DATASET_SCHEMA__".document FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_task_document_command();

CREATE TRIGGER catalog_task_metadata INSTEAD OF INSERT OR UPDATE OR DELETE ON "__DATASET_SCHEMA__".metadata FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_task_metadata_command();

CREATE TRIGGER catalog_task_classification INSTEAD OF INSERT OR UPDATE OR DELETE ON "__DATASET_SCHEMA__".classification FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_task_classification_command();

CREATE TRIGGER catalog_task_identity INSTEAD OF INSERT OR UPDATE OR DELETE ON "__CATALOG_SCHEMA__".normalization_canonicals FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_task_identity_command();

CREATE TRIGGER catalog_task_alias INSTEAD OF INSERT OR UPDATE OR DELETE ON "__CATALOG_SCHEMA__".normalization_aliases FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_task_alias_command();

CREATE TRIGGER catalog_task_collection INSTEAD OF INSERT OR UPDATE OR DELETE ON "__CATALOG_SCHEMA__".library_collections FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_task_collection_command();

CREATE TRIGGER catalog_task_membership INSTEAD OF INSERT OR UPDATE OR DELETE ON "__CATALOG_SCHEMA__".library_collection_items FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_task_membership_command();


CREATE TRIGGER catalog_task_preview AFTER INSERT OR UPDATE ON "__CATALOG_SCHEMA__".library_book_previews FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_task_preview_command();
CREATE TRIGGER catalog_task_suggestion AFTER INSERT OR UPDATE OR DELETE ON "__CATALOG_SCHEMA__".normalization_suggestions FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_task_review_command();
CREATE TRIGGER catalog_task_publisher_proposal AFTER INSERT OR UPDATE OR DELETE ON "__CATALOG_SCHEMA__".publisher_merge_proposals FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_task_review_command();
CREATE TRIGGER catalog_review_decision AFTER UPDATE ON "__CATALOG_SCHEMA__".catalog_proposals FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_project_review_decision();
CREATE TRIGGER catalog_task_separation AFTER INSERT OR DELETE ON "__CATALOG_SCHEMA__".publisher_separations FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_separation_command();
CREATE TRIGGER catalog_separation_projection AFTER INSERT OR DELETE ON "__CATALOG_SCHEMA__".catalog_separations FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_separation_command();


-- Keep the retained review inventory aligned with explicit catalog alias edits.
-- Task command triggers maintain their own richer AI checkpoint payloads.
CREATE FUNCTION "__CATALOG_SCHEMA__".catalog_refresh_alias_review(observed BIGINT)
RETURNS VOID LANGUAGE plpgsql SET search_path="__CATALOG_SCHEMA__",public AS $fn$
DECLARE targets BIGINT[]; role TEXT; raw TEXT; kind TEXT;
BEGIN
 SELECT n.raw_name,n.kind INTO raw,kind FROM catalog_names n WHERE name_id=observed;
 IF raw IS NULL THEN RETURN; END IF;
 SELECT array_agg(DISTINCT a.entity_id) INTO targets FROM catalog_aliases a JOIN catalog_entities e USING(entity_id)
 WHERE a.name_id=observed AND e.status='active';
 role=CASE WHEN kind='organization' OR EXISTS(SELECT 1 FROM catalog_aliases a JOIN catalog_entity_roles r USING(entity_id)
   WHERE a.name_id=observed AND r.role='publisher') OR EXISTS(SELECT 1 FROM catalog_aliases a JOIN catalog_contributions c USING(entity_id) WHERE a.name_id=observed AND c.role='publisher') THEN 'publisher' ELSE 'personality' END;
 IF NOT EXISTS(SELECT 1 FROM catalog_alias_reviews WHERE name_id=observed) THEN
   INSERT INTO catalog_alias_reviews(name_id,entity_type,normalized_name,script_label,source,created_at)
   VALUES(observed,role,lower(raw),'other','catalog.owner',CURRENT_TIMESTAMP::text);
 END IF;
 UPDATE catalog_alias_reviews SET entity_id=CASE WHEN cardinality(targets)=1 THEN targets[1] END,
   decision_status=CASE WHEN cardinality(targets)=1 THEN 'linked' ELSE 'pending' END,updated_at=CURRENT_TIMESTAMP::text
 WHERE name_id=observed;
END $fn$;
CREATE FUNCTION "__CATALOG_SCHEMA__".catalog_alias_review_sync()
RETURNS TRIGGER LANGUAGE plpgsql SET search_path="__CATALOG_SCHEMA__",public AS $fn$
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
END $fn$;
CREATE TRIGGER catalog_alias_review_sync AFTER INSERT OR UPDATE OR DELETE ON "__CATALOG_SCHEMA__".catalog_aliases
 FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_alias_review_sync();
CREATE TRIGGER catalog_entity_review_sync AFTER UPDATE OF status ON "__CATALOG_SCHEMA__".catalog_entities
 FOR EACH ROW WHEN(OLD.status IS DISTINCT FROM NEW.status) EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_alias_review_sync();

CREATE FUNCTION "__CATALOG_SCHEMA__".catalog_command_snapshot_guard()
RETURNS TRIGGER LANGUAGE plpgsql SET search_path="__CATALOG_SCHEMA__","__DATASET_SCHEMA__",public AS $fn$
DECLARE key_column TEXT; root REGCLASS; root_key TEXT; previous JSONB; current_row JSONB; key_value TEXT;
BEGIN
 CASE TG_TABLE_NAME
 WHEN 'document' THEN key_column='md5';root='catalog_documents';root_key='md5';
 WHEN 'metadata' THEN key_column='md5';root='catalog_documents';root_key='md5';
 WHEN 'classification' THEN key_column='id';root='catalog_classifications';root_key='classification_id';
 WHEN 'normalization_canonicals' THEN key_column='canonical_id';root='catalog_entities';root_key='entity_id';
 WHEN 'normalization_aliases' THEN key_column='alias_id';root='catalog_alias_reviews';root_key='alias_id';
 WHEN 'library_collections' THEN key_column='collection_id';root='catalog_collections';root_key='collection_id';
 WHEN 'library_collection_items' THEN key_column='md5';root='catalog_documents';root_key='md5';
 ELSE RAISE EXCEPTION 'unknown catalog command view'; END CASE;
 previous=to_jsonb(OLD);key_value=previous->>key_column;
 IF TG_OP='UPDATE' AND to_jsonb(NEW)->>key_column IS DISTINCT FROM key_value THEN
   RAISE EXCEPTION 'catalog command identity is immutable';
 END IF;
 EXECUTE format('SELECT to_jsonb(r) FROM %s r WHERE %I::text=$1 FOR UPDATE',root,root_key) USING key_value;
 IF TG_TABLE_NAME IN ('document','metadata','library_collection_items') THEN
   PERFORM 1 FROM catalog_publications WHERE publication_id=(SELECT publication_id FROM catalog_documents WHERE md5=key_value) FOR UPDATE;
 END IF;
 EXECUTE format('SELECT to_jsonb(r) FROM %I.%I r WHERE %I::text=$1',TG_TABLE_SCHEMA,TG_TABLE_NAME,key_column) INTO current_row USING key_value;
 IF current_row IS DISTINCT FROM previous THEN
   RAISE EXCEPTION 'catalog record changed; reload before retrying' USING ERRCODE='40001';
 END IF;
 RETURN coalesce(NEW,OLD);
END $fn$;
CREATE TRIGGER catalog_guard_snapshot INSTEAD OF UPDATE OR DELETE ON "__DATASET_SCHEMA__".document FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_command_snapshot_guard();
CREATE TRIGGER catalog_guard_snapshot INSTEAD OF UPDATE OR DELETE ON "__DATASET_SCHEMA__".metadata FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_command_snapshot_guard();
CREATE TRIGGER catalog_guard_snapshot INSTEAD OF UPDATE OR DELETE ON "__DATASET_SCHEMA__".classification FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_command_snapshot_guard();
CREATE TRIGGER catalog_guard_snapshot INSTEAD OF UPDATE OR DELETE ON "__CATALOG_SCHEMA__".normalization_canonicals FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_command_snapshot_guard();
CREATE TRIGGER catalog_guard_snapshot INSTEAD OF UPDATE OR DELETE ON "__CATALOG_SCHEMA__".normalization_aliases FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_command_snapshot_guard();
CREATE TRIGGER catalog_guard_snapshot INSTEAD OF UPDATE OR DELETE ON "__CATALOG_SCHEMA__".library_collections FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_command_snapshot_guard();
CREATE TRIGGER catalog_guard_snapshot INSTEAD OF UPDATE OR DELETE ON "__CATALOG_SCHEMA__".library_collection_items FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_command_snapshot_guard();
