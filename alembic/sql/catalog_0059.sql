-- Repair composite identities omitted by the initial deployed catalog.
DO $repair$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='"__CATALOG_SCHEMA__".catalog_entity_roles'::regclass AND contype='p') THEN
 ALTER TABLE "__CATALOG_SCHEMA__".catalog_entity_roles ADD PRIMARY KEY ("entity_id","role");
 END IF;
END $repair$;
DO $repair$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='"__CATALOG_SCHEMA__".catalog_identifiers'::regclass AND contype='p') THEN
 ALTER TABLE "__CATALOG_SCHEMA__".catalog_identifiers ADD PRIMARY KEY ("publication_id","kind","position");
 END IF;
END $repair$;
DO $repair$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='"__CATALOG_SCHEMA__".catalog_genres'::regclass AND contype='p') THEN
 ALTER TABLE "__CATALOG_SCHEMA__".catalog_genres ADD PRIMARY KEY ("publication_id","position");
 END IF;
END $repair$;
DO $repair$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='"__CATALOG_SCHEMA__".catalog_subjects'::regclass AND contype='p') THEN
 ALTER TABLE "__CATALOG_SCHEMA__".catalog_subjects ADD PRIMARY KEY ("publication_id","position");
 END IF;
END $repair$;
DO $repair$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='"__CATALOG_SCHEMA__".catalog_audiences'::regclass AND contype='p') THEN
 ALTER TABLE "__CATALOG_SCHEMA__".catalog_audiences ADD PRIMARY KEY ("publication_id","position");
 END IF;
END $repair$;
DO $repair$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='"__CATALOG_SCHEMA__".catalog_sufficient_modes'::regclass AND contype='p') THEN
 ALTER TABLE "__CATALOG_SCHEMA__".catalog_sufficient_modes ADD PRIMARY KEY ("md5","position");
 END IF;
END $repair$;
DO $repair$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='"__CATALOG_SCHEMA__".catalog_references'::regclass AND contype='p') THEN
 ALTER TABLE "__CATALOG_SCHEMA__".catalog_references ADD PRIMARY KEY ("publication_id");
 END IF;
END $repair$;
DO $repair$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='"__CATALOG_SCHEMA__".catalog_reference_authors'::regclass AND contype='p') THEN
 ALTER TABLE "__CATALOG_SCHEMA__".catalog_reference_authors ADD PRIMARY KEY ("publication_id","position");
 END IF;
END $repair$;
DO $repair$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='"__CATALOG_SCHEMA__".catalog_preview_pages'::regclass AND contype='p') THEN
 ALTER TABLE "__CATALOG_SCHEMA__".catalog_preview_pages ADD PRIMARY KEY ("request_id","role");
 END IF;
END $repair$;
-- Frozen preservation fields for a coordinated retirement, without source copies.
ALTER TABLE "__CATALOG_SCHEMA__".catalog_collections ADD COLUMN metadata_template_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE "__CATALOG_SCHEMA__".catalog_collections ADD COLUMN applied_at TEXT;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_collections ADD COLUMN created_at TEXT;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_collections ADD COLUMN normalized_title TEXT;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_collections ADD COLUMN source_updated_at TEXT;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_classifications ADD COLUMN created_by TEXT NOT NULL DEFAULT 'gemini';
ALTER TABLE "__CATALOG_SCHEMA__".catalog_classifications ADD COLUMN created_at TIMESTAMP;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_publications ADD COLUMN collection_item_title TEXT;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_publications ADD COLUMN collection_created_at TEXT;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_publications ADD COLUMN collection_updated_at TEXT;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_publications ADD COLUMN has_metadata BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_publications ADD COLUMN metadata_present BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_entities ADD COLUMN normalized_name TEXT;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_entities ADD COLUMN created_at TEXT;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_entities ADD COLUMN source_updated_at TEXT;
CREATE TABLE "__CATALOG_SCHEMA__".catalog_alias_reviews (
	alias_id BIGSERIAL NOT NULL,
	name_id BIGINT NOT NULL,
	entity_type TEXT NOT NULL,
	entity_id BIGINT,
	normalized_name TEXT NOT NULL,
	script_label TEXT NOT NULL,
	decision_status TEXT DEFAULT 'pending' NOT NULL,
	docs_count BIGINT DEFAULT '0' NOT NULL,
	mentions_count BIGINT DEFAULT '0' NOT NULL,
	marker_count BIGINT DEFAULT '0' NOT NULL,
	confidence FLOAT,
	source TEXT,
	reason TEXT,
	created_at TEXT,
	updated_at TEXT,
	successful_model TEXT,
	prompt_version TEXT,
	schema_version TEXT,
	surname_full TEXT,
	surname_initials TEXT,
	name_full TEXT,
	name_initials TEXT,
	father_name_full TEXT,
	father_name_initials TEXT,
	title TEXT,
	sex TEXT,
	source_roles JSONB DEFAULT '[]' NOT NULL,
	PRIMARY KEY (alias_id),
	UNIQUE (entity_type, name_id),
	FOREIGN KEY(name_id) REFERENCES "__CATALOG_SCHEMA__".catalog_names (name_id) ON DELETE RESTRICT,
	FOREIGN KEY(entity_id) REFERENCES "__CATALOG_SCHEMA__".catalog_entities (entity_id) ON DELETE RESTRICT
);
CREATE INDEX idx_catalog_alias_reviews_entity ON "__CATALOG_SCHEMA__".catalog_alias_reviews(entity_id);

ALTER TABLE "__CATALOG_SCHEMA__".catalog_classifications DROP CONSTRAINT catalog_classifications_node_id_key;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_classifications ADD CONSTRAINT catalog_classifications_node_id_key UNIQUE(node_id) DEFERRABLE INITIALLY IMMEDIATE;

-- Explicit upserts work with both the pre-cutover source and catalog command
-- views. All identifiers come from this registry; JSON values remain parameters.
CREATE FUNCTION "__CATALOG_SCHEMA__".catalog_upsert(target TEXT, payload JSONB, conflict_fields TEXT[],
    update_fields TEXT[] DEFAULT NULL, prefer_existing TEXT[] DEFAULT '{}')
RETURNS JSONB LANGUAGE plpgsql
SET search_path="__CATALOG_SCHEMA__",public AS $fn$
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
END $fn$;
