-- Lossless relational decomposition. Execute only as part of revision 0061.
-- The transaction retains existing identities, order, and empty-list presence.
SET LOCAL lock_timeout = '10s';
SET LOCAL jit = off;
LOCK TABLE "__CATALOG_SCHEMA__".catalog_publications,
 "__CATALOG_SCHEMA__".catalog_documents,"__CATALOG_SCHEMA__".catalog_contributions,
 "__CATALOG_SCHEMA__".catalog_sufficient_modes,"__CATALOG_SCHEMA__".catalog_references
 IN SHARE ROW EXCLUSIVE MODE;

DO $conflicts$ BEGIN
 IF EXISTS(SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_imports
   WHERE state<>'active' OR manifest->>'legacy_retired' IS DISTINCT FROM 'true') THEN
   RAISE EXCEPTION 'catalog normalization requires completed physical-table retirement or an empty catalog';
 END IF;
 IF EXISTS(SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_contributions
   GROUP BY publication_id,role,position HAVING count(DISTINCT role_name)>1
     OR (bool_or(role_name IS NULL) AND bool_or(role_name IS NOT NULL))) THEN
   RAISE EXCEPTION 'credit-group labels conflict; review existing rows before normalization';
 END IF;
END $conflicts$;

CREATE TABLE "__CATALOG_SCHEMA__".catalog_publication_languages (
 publication_id BIGINT NOT NULL,
 position INTEGER NOT NULL, language TEXT NOT NULL,
 PRIMARY KEY(publication_id,position),
 CONSTRAINT ck_catalog_language_position CHECK(position >= 0),
 CONSTRAINT ck_catalog_language_value CHECK(btrim(language) <> '')
);
CREATE TABLE "__CATALOG_SCHEMA__".catalog_document_access_modes (
 md5 TEXT NOT NULL,
 position INTEGER NOT NULL, mode TEXT NOT NULL,
 PRIMARY KEY(md5,position),
 CONSTRAINT ck_catalog_access_mode_position CHECK(position >= 0),
 CONSTRAINT ck_catalog_access_mode_value CHECK(mode IN ('auditory','tactile','textual','visual'))
);
CREATE TABLE "__CATALOG_SCHEMA__".catalog_sufficient_mode_items (
 md5 TEXT NOT NULL, group_position INTEGER NOT NULL, position INTEGER NOT NULL, mode TEXT NOT NULL,
 PRIMARY KEY(md5,group_position,position),
 CONSTRAINT ck_catalog_sufficient_mode_positions CHECK(group_position >= 0 AND position >= 0),
 CONSTRAINT ck_catalog_sufficient_mode_value CHECK(mode IN ('auditory','tactile','textual','visual'))
);
ALTER TABLE "__CATALOG_SCHEMA__".catalog_references ADD COLUMN urls_present BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_sufficient_modes
 ADD CONSTRAINT ck_catalog_sufficient_group_position CHECK(position >= 0);
CREATE TABLE "__CATALOG_SCHEMA__".catalog_reference_urls (
 publication_id BIGINT NOT NULL,
 position INTEGER NOT NULL, url TEXT NOT NULL,
 PRIMARY KEY(publication_id,position),
 CONSTRAINT ck_catalog_reference_url_position CHECK(position >= 0)
);
CREATE TABLE "__CATALOG_SCHEMA__".catalog_credit_groups (
 publication_id BIGINT NOT NULL,
 role TEXT NOT NULL, position INTEGER NOT NULL, role_name TEXT,
 PRIMARY KEY(publication_id,role,position),
 CONSTRAINT ck_catalog_credit_group_position CHECK(position >= 0)
);

INSERT INTO "__CATALOG_SCHEMA__".catalog_publication_languages
 SELECT p.publication_id,(item.ordinality-1)::integer,item.value
 FROM "__CATALOG_SCHEMA__".catalog_publications p CROSS JOIN LATERAL unnest(p.languages) WITH ORDINALITY item(value,ordinality);
INSERT INTO "__CATALOG_SCHEMA__".catalog_document_access_modes
 SELECT d.md5,(item.ordinality-1)::integer,item.value
 FROM "__CATALOG_SCHEMA__".catalog_documents d CROSS JOIN LATERAL unnest(d.access_modes) WITH ORDINALITY item(value,ordinality);
INSERT INTO "__CATALOG_SCHEMA__".catalog_sufficient_mode_items
 SELECT g.md5,g.position,(item.ordinality-1)::integer,item.value
 FROM "__CATALOG_SCHEMA__".catalog_sufficient_modes g CROSS JOIN LATERAL unnest(g.modes) WITH ORDINALITY item(value,ordinality);
UPDATE "__CATALOG_SCHEMA__".catalog_references SET urls_present=urls IS NOT NULL;
INSERT INTO "__CATALOG_SCHEMA__".catalog_reference_urls
 SELECT r.publication_id,(item.ordinality-1)::integer,item.value
 FROM "__CATALOG_SCHEMA__".catalog_references r CROSS JOIN LATERAL unnest(r.urls) WITH ORDINALITY item(value,ordinality);
INSERT INTO "__CATALOG_SCHEMA__".catalog_credit_groups
 SELECT publication_id,role,position,min(role_name) FROM "__CATALOG_SCHEMA__".catalog_contributions
 GROUP BY publication_id,role,position;

-- Validate referential integrity after the bulk load, avoiding one pending
-- foreign-key trigger event per inserted row. All DDL/data remains atomic.
ALTER TABLE "__CATALOG_SCHEMA__".catalog_publication_languages
 ADD FOREIGN KEY(publication_id) REFERENCES "__CATALOG_SCHEMA__".catalog_publications(publication_id) ON DELETE CASCADE;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_document_access_modes
 ADD FOREIGN KEY(md5) REFERENCES "__CATALOG_SCHEMA__".catalog_documents(md5) ON DELETE CASCADE;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_sufficient_mode_items
 ADD CONSTRAINT fk_catalog_sufficient_mode_group FOREIGN KEY(md5,group_position)
 REFERENCES "__CATALOG_SCHEMA__".catalog_sufficient_modes(md5,position) ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_reference_urls
 ADD FOREIGN KEY(publication_id) REFERENCES "__CATALOG_SCHEMA__".catalog_references(publication_id) ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_credit_groups
 ADD FOREIGN KEY(publication_id) REFERENCES "__CATALOG_SCHEMA__".catalog_publications(publication_id) ON DELETE CASCADE;

DO $verify$ BEGIN
 IF EXISTS(SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_publications p
   WHERE p.languages IS DISTINCT FROM ARRAY(SELECT language FROM "__CATALOG_SCHEMA__".catalog_publication_languages
     WHERE publication_id=p.publication_id ORDER BY position)) THEN
   RAISE EXCEPTION 'publication language decomposition is not lossless'; END IF;
 IF EXISTS(SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_documents d
   WHERE d.access_modes IS DISTINCT FROM ARRAY(SELECT mode FROM "__CATALOG_SCHEMA__".catalog_document_access_modes
     WHERE md5=d.md5 ORDER BY position)) THEN
   RAISE EXCEPTION 'document access-mode decomposition is not lossless'; END IF;
 IF EXISTS(SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_sufficient_modes g
   WHERE g.modes IS DISTINCT FROM ARRAY(SELECT mode FROM "__CATALOG_SCHEMA__".catalog_sufficient_mode_items
     WHERE md5=g.md5 AND group_position=g.position ORDER BY position)) THEN
   RAISE EXCEPTION 'sufficient-mode decomposition is not lossless'; END IF;
 IF EXISTS(SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_references r
   WHERE r.urls IS DISTINCT FROM CASE WHEN r.urls_present THEN ARRAY(
     SELECT url FROM "__CATALOG_SCHEMA__".catalog_reference_urls
     WHERE publication_id=r.publication_id ORDER BY position) END) THEN
   RAISE EXCEPTION 'reference URL decomposition is not lossless'; END IF;
 IF EXISTS(SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_contributions c
   JOIN "__CATALOG_SCHEMA__".catalog_credit_groups g USING(publication_id,role,position)
   WHERE c.role_name IS DISTINCT FROM g.role_name) THEN
   RAISE EXCEPTION 'credit-group decomposition is not lossless'; END IF;
END $verify$;

-- Read projections preserve the previous bibliographic envelope. They contain
-- no independent domain rows. Scalar columns remain automatically updatable;
-- collection-valued writes require adaptation to their child relations.
DO $projections$
DECLARE item RECORD; projection TEXT;
BEGIN
 FOR item IN SELECT * FROM (VALUES
   ('catalog_publications','catalog_publication_metadata','languages',
     'ARRAY(SELECT language FROM "__CATALOG_SCHEMA__".catalog_publication_languages WHERE publication_id=p.publication_id ORDER BY position)'),
   ('catalog_documents','catalog_document_metadata','access_modes',
     'ARRAY(SELECT mode FROM "__CATALOG_SCHEMA__".catalog_document_access_modes WHERE md5=p.md5 ORDER BY position)'),
   ('catalog_sufficient_modes','catalog_sufficient_mode_metadata','modes',
     'ARRAY(SELECT mode FROM "__CATALOG_SCHEMA__".catalog_sufficient_mode_items WHERE md5=p.md5 AND group_position=p.position ORDER BY position)'),
   ('catalog_references','catalog_reference_metadata','urls',
     'CASE WHEN p.urls_present THEN ARRAY(SELECT url FROM "__CATALOG_SCHEMA__".catalog_reference_urls WHERE publication_id=p.publication_id ORDER BY position) END'),
   ('catalog_contributions','catalog_contribution_metadata','role_name',
     '(SELECT role_name FROM "__CATALOG_SCHEMA__".catalog_credit_groups WHERE publication_id=p.publication_id AND role=p.role AND position=p.position)')
 ) AS projections(table_name,view_name,field,expression) LOOP
   SELECT string_agg(CASE WHEN a.attname=item.field THEN format('%s AS %I',item.expression,a.attname)
     ELSE format('p.%I',a.attname) END,',' ORDER BY a.attnum) INTO projection
   FROM pg_attribute a WHERE a.attrelid=to_regclass(format('%I.%I','__CATALOG_SCHEMA__',item.table_name))
     AND a.attnum>0 AND NOT a.attisdropped AND a.attname<>'urls_present';
   EXECUTE format('CREATE VIEW %I.%I AS SELECT %s FROM %I.%I p',
     '__CATALOG_SCHEMA__',item.view_name,projection,'__CATALOG_SCHEMA__',item.table_name);
 END LOOP;
END $projections$;

ALTER TABLE "__CATALOG_SCHEMA__".catalog_contributions
 ADD CONSTRAINT fk_catalog_credit_group FOREIGN KEY(publication_id,role,position)
 REFERENCES "__CATALOG_SCHEMA__".catalog_credit_groups(publication_id,role,position) ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_contributions
 DROP CONSTRAINT catalog_contributions_publication_id_fkey RESTRICT;
