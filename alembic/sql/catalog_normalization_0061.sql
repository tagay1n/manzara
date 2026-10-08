-- PostgreSQL owns every derived key, including writes through adapter views,
-- COPY, direct SQL, and future backend clients. No Python normalization branch.
CREATE FUNCTION "__CATALOG_SCHEMA__".catalog_name_key(value TEXT)
RETURNS TEXT LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $fn$
 SELECT casefold(value COLLATE pg_catalog.pg_unicode_fast)
$fn$;
CREATE FUNCTION "__CATALOG_SCHEMA__".catalog_title_key(value TEXT)
RETURNS TEXT LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $fn$
 SELECT btrim(regexp_replace("__CATALOG_SCHEMA__".catalog_name_key(value),
   '[^0-9a-zа-яёәҗңөүһіғқҫ]+',' ','g'))
$fn$;
CREATE FUNCTION "__CATALOG_SCHEMA__".catalog_identifier_key(kind TEXT,value TEXT)
RETURNS TEXT LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $fn$
 SELECT CASE WHEN kind='isbn' THEN regexp_replace(upper(value COLLATE pg_catalog.pg_unicode_fast),'[^0-9X]','','g') ELSE value END
$fn$;

CREATE FUNCTION "__CATALOG_SCHEMA__".catalog_derive_keys()
RETURNS TRIGGER LANGUAGE plpgsql AS $fn$
BEGIN
 CASE TG_TABLE_NAME
 WHEN 'catalog_entities' THEN NEW.normalized_name="__CATALOG_SCHEMA__".catalog_name_key(NEW.display_name);
 WHEN 'catalog_collections' THEN NEW.normalized_title="__CATALOG_SCHEMA__".catalog_title_key(NEW.title);
 WHEN 'catalog_identifiers' THEN NEW.normalized="__CATALOG_SCHEMA__".catalog_identifier_key(NEW.kind,NEW.value);
 ELSE RAISE EXCEPTION 'unsupported derived-key relation';
 END CASE;
 RETURN NEW;
END $fn$;
CREATE TRIGGER catalog_derive_entity_key BEFORE INSERT OR UPDATE ON "__CATALOG_SCHEMA__".catalog_entities
 FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_derive_keys();
CREATE TRIGGER catalog_derive_collection_key BEFORE INSERT OR UPDATE ON "__CATALOG_SCHEMA__".catalog_collections
 FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_derive_keys();
CREATE TRIGGER catalog_derive_identifier_key BEFORE INSERT OR UPDATE ON "__CATALOG_SCHEMA__".catalog_identifiers
 FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_derive_keys();

-- Record the original values before adopting the canonical derivation rules.
WITH previous AS MATERIALIZED (
 SELECT * FROM "__CATALOG_SCHEMA__".catalog_entities
 WHERE normalized_name IS DISTINCT FROM "__CATALOG_SCHEMA__".catalog_name_key(display_name)
), changed AS (
 UPDATE "__CATALOG_SCHEMA__".catalog_entities e SET normalized_name="__CATALOG_SCHEMA__".catalog_name_key(e.display_name),
   revision=e.revision+1,updated_at=CURRENT_TIMESTAMP FROM previous p WHERE e.entity_id=p.entity_id RETURNING e.*
) INSERT INTO "__CATALOG_SCHEMA__".catalog_revisions(record_kind,record_key,actor,"before","after")
 SELECT 'entity',p.entity_id::text,'schema-migration:0061',to_jsonb(p),to_jsonb(c) FROM previous p JOIN changed c USING(entity_id);
WITH previous AS MATERIALIZED (
 SELECT * FROM "__CATALOG_SCHEMA__".catalog_collections
 WHERE normalized_title IS DISTINCT FROM "__CATALOG_SCHEMA__".catalog_title_key(title)
), changed AS (
 UPDATE "__CATALOG_SCHEMA__".catalog_collections e SET normalized_title="__CATALOG_SCHEMA__".catalog_title_key(e.title),
   revision=e.revision+1,updated_at=CURRENT_TIMESTAMP FROM previous p WHERE e.collection_id=p.collection_id RETURNING e.*
) INSERT INTO "__CATALOG_SCHEMA__".catalog_revisions(record_kind,record_key,actor,"before","after")
 SELECT 'collection',p.collection_id::text,'schema-migration:0061',to_jsonb(p),to_jsonb(c) FROM previous p JOIN changed c USING(collection_id);
WITH previous AS MATERIALIZED (
 SELECT * FROM "__CATALOG_SCHEMA__".catalog_identifiers
 WHERE normalized IS DISTINCT FROM "__CATALOG_SCHEMA__".catalog_identifier_key(kind,value)
), changed AS (
 UPDATE "__CATALOG_SCHEMA__".catalog_identifiers e SET normalized="__CATALOG_SCHEMA__".catalog_identifier_key(e.kind,e.value)
 FROM previous p WHERE e.publication_id=p.publication_id AND e.kind=p.kind AND e.position=p.position RETURNING e.*
) INSERT INTO "__CATALOG_SCHEMA__".catalog_revisions(record_kind,record_key,actor,"before","after")
 SELECT 'identifier',concat(p.publication_id,':',p.kind,':',p.position),'schema-migration:0061',to_jsonb(p),to_jsonb(c)
 FROM previous p JOIN changed c USING(publication_id,kind,position);
