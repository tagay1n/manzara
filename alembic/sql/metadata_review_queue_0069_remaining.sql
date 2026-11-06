-- Stop CLI and scheduled catalog writers. Requires free space and write access.
-- Run ONLY query 1 repeatedly, committing each execution, until updated_rows=0.
-- Then run queries 2-4 once. Finally restart CLI to validate and record revision.
-- PostgreSQL UPDATE creates row versions/WAL; batching does not eliminate space needs.

-- 1. One bounded, resumable backfill batch.
WITH batch AS (
    SELECT publication_id FROM monocorpus.catalog_publications
    WHERE metadata_present AND metadata_review_order=0
    ORDER BY publication_id LIMIT 500
), updated AS (
    UPDATE monocorpus.catalog_publications p SET metadata_review_order=1
    FROM batch WHERE p.publication_id=batch.publication_id
      AND p.metadata_present AND p.metadata_review_order=0
    RETURNING p.publication_id
)
SELECT COUNT(*) AS updated_rows FROM updated;

-- 2. Create the missing sequence.
CREATE SEQUENCE IF NOT EXISTS monocorpus.catalog_metadata_review_order_seq
    AS bigint START WITH 2;

-- 3. Associate its lifecycle with the queue column.
ALTER SEQUENCE monocorpus.catalog_metadata_review_order_seq
    OWNED BY monocorpus.catalog_publications.metadata_review_order;

-- 4. Ensure allocation starts beyond every existing queue position; never rewind.
SELECT setval('monocorpus.catalog_metadata_review_order_seq'::regclass,
    GREATEST(
        (SELECT COALESCE(MAX(metadata_review_order),0)+1 FROM monocorpus.catalog_publications),
        (SELECT last_value + CASE WHEN is_called THEN 1 ELSE 0 END
            FROM monocorpus.catalog_metadata_review_order_seq),
        2), false);
