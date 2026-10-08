-- Constraints validate existing rows. Conflicts abort the transaction without
-- deleting, merging, reordering, or silently repairing durable records.
SET LOCAL lock_timeout = '10s';

ALTER TABLE "__CATALOG_SCHEMA__".catalog_classification_nodes
 ADD CONSTRAINT ck_catalog_node_parent CHECK (parent_id IS NULL OR parent_id <> node_id);
ALTER TABLE "__CATALOG_SCHEMA__".catalog_publications
 ADD CONSTRAINT ck_catalog_publication_merge_target CHECK (merged_into_id IS NULL OR merged_into_id <> publication_id);
ALTER TABLE "__CATALOG_SCHEMA__".catalog_entities
 ADD CONSTRAINT ck_catalog_entity_merge_target CHECK (merged_into_id IS NULL OR merged_into_id <> entity_id),
 ADD CONSTRAINT ck_catalog_entity_merge_status CHECK ((status = 'merged') = (merged_into_id IS NOT NULL));
ALTER TABLE "__CATALOG_SCHEMA__".catalog_names
 ADD CONSTRAINT ck_catalog_name_kind CHECK (kind IN ('person','organization','unknown'));
ALTER TABLE "__CATALOG_SCHEMA__".catalog_aliases
 ADD CONSTRAINT ck_catalog_alias_approval CHECK (approval IN ('unconfirmed','confirmed'));
ALTER TABLE "__CATALOG_SCHEMA__".catalog_contributions
 ADD CONSTRAINT uq_catalog_credit_slot UNIQUE (publication_id,role,position,nested_position),
 ADD CONSTRAINT ck_catalog_credit_positions CHECK (position >= 0 AND nested_position >= 0),
 ADD CONSTRAINT ck_catalog_credit_resolution CHECK (resolution IN ('unconfirmed','confirmed')),
 ADD CONSTRAINT ck_catalog_credit_entity CHECK (resolution <> 'confirmed' OR entity_id IS NOT NULL);
ALTER TABLE "__CATALOG_SCHEMA__".catalog_audiences
 ADD CONSTRAINT ck_catalog_audience_min_age CHECK (min_age IS NULL OR min_age >= 0),
 ADD CONSTRAINT ck_catalog_audience_max_age CHECK (max_age IS NULL OR max_age >= 0),
 ADD CONSTRAINT ck_catalog_audience_age_range CHECK (min_age IS NULL OR max_age IS NULL OR min_age <= max_age);

CREATE UNIQUE INDEX uq_catalog_proposal_entity
 ON "__CATALOG_SCHEMA__".catalog_proposal_members(proposal_id,entity_id) WHERE entity_id IS NOT NULL;
CREATE UNIQUE INDEX uq_catalog_proposal_name
 ON "__CATALOG_SCHEMA__".catalog_proposal_members(proposal_id,name_id) WHERE name_id IS NOT NULL;

-- Install and validate the stronger parent relation before retiring the old FK.
ALTER TABLE "__CATALOG_SCHEMA__".catalog_reference_authors
 ADD CONSTRAINT fk_catalog_reference_authors_reference FOREIGN KEY(publication_id)
 REFERENCES "__CATALOG_SCHEMA__".catalog_references(publication_id) ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "__CATALOG_SCHEMA__".catalog_reference_authors
 DROP CONSTRAINT catalog_reference_authors_publication_id_fkey RESTRICT;

-- Only remove known duplicate constraints after proving that a primary key
-- enforces the identical columns. RESTRICT protects unexpected dependents.
DO $duplicates$
DECLARE item RECORD; relation REGCLASS;
BEGIN
 FOR item IN SELECT * FROM (VALUES
   ('catalog_entity_roles','catalog_entity_roles_entity_id_role_key'),
   ('catalog_identifiers','catalog_identifiers_publication_id_kind_position_key'),
   ('catalog_preview_pages','catalog_preview_pages_request_id_role_key')
 ) AS duplicates(table_name,constraint_name) LOOP
   relation=to_regclass(format('%I.%I','__CATALOG_SCHEMA__',item.table_name));
   IF EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid=relation AND conname=item.constraint_name) THEN
     IF NOT EXISTS(SELECT 1 FROM pg_constraint u JOIN pg_constraint p
       ON p.conrelid=u.conrelid AND p.contype='p' AND p.conkey=u.conkey
       WHERE u.conrelid=relation AND u.conname=item.constraint_name
         AND u.contype='u' AND NOT u.condeferrable) THEN
       RAISE EXCEPTION 'constraint % is not a redundant unique constraint',item.constraint_name;
     END IF;
     EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I RESTRICT',relation,item.constraint_name);
   END IF;
 END LOOP;
END $duplicates$;
