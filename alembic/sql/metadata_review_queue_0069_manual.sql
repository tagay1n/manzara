-- Six standalone statements for web editors that autocommit each query.
-- Stop the CLI and scheduled catalog writers before running. Restore free
-- space and normal write access first. Each successful statement persists;
-- after an error, fix its cause and rerun. CLI startup validates compatibility.
-- Does not stamp Alembic. Restart the CLI after all statements succeed.
ALTER TABLE monocorpus.catalog_publications
    ADD COLUMN IF NOT EXISTS metadata_review_order bigint NOT NULL DEFAULT 0
    CONSTRAINT ck_catalog_metadata_review_order CHECK (metadata_review_order >= -1);
UPDATE monocorpus.catalog_publications SET metadata_review_order=1
    WHERE metadata_present AND metadata_review_order=0;
CREATE SEQUENCE IF NOT EXISTS monocorpus.catalog_metadata_review_order_seq
    AS bigint START WITH 2;
ALTER SEQUENCE monocorpus.catalog_metadata_review_order_seq
    OWNED BY monocorpus.catalog_publications.metadata_review_order;
SELECT setval('monocorpus.catalog_metadata_review_order_seq'::regclass,
    GREATEST(
        (SELECT COALESCE(MAX(metadata_review_order),0)+1 FROM monocorpus.catalog_publications),
        (SELECT last_value + CASE WHEN is_called THEN 1 ELSE 0 END
            FROM monocorpus.catalog_metadata_review_order_seq),
        2), false);
SELECT 'Metadata queue prepared - restart CLI to verify and record revision' AS result;
