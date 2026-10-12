-- Counts are derived before the language join, preserving one mention per credit/document.
WITH observations AS (
    SELECT {{compact_name}} AS raw_name, d.md5, c.role, p.publication_id
    FROM catalog_names n
    JOIN catalog_contributions c USING(name_id)
    JOIN catalog_publications p USING(publication_id)
    JOIN catalog_documents d USING(publication_id)
    WHERE n.kind='person' AND c.role IN ('author','editor','translator','illustrator','contributor')
      AND d.inclusion='included' AND p.has_metadata AND p.metadata_present
), selected_observations AS (
    SELECT * FROM observations WHERE raw_name<>'' AND ({{name_filter}})
), people AS (
    SELECT raw_name, count(DISTINCT md5) AS document_count, count(*) AS mention_count,
           jsonb_agg(DISTINCT role COLLATE "C" ORDER BY role COLLATE "C") AS source_roles,
           array_agg(DISTINCT publication_id) AS publication_ids
    FROM selected_observations GROUP BY raw_name
), languages AS (
    SELECT people.*, COALESCE(hints.document_languages,'[]'::jsonb) AS document_languages
    FROM people
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(language ORDER BY lower(language) COLLATE "C",language COLLATE "C") AS document_languages
        FROM (SELECT DISTINCT {{compact_language}} AS language
              FROM catalog_publication_languages l
              WHERE l.publication_id=ANY(people.publication_ids)) language_values
        WHERE language COLLATE "C" ~ '^[A-Za-z0-9-]{1,35}$'
    ) hints ON true
)
SELECT raw_name, document_count, mention_count, source_roles, document_languages,
       encode(sha256(convert_to('[' || to_json(raw_name)::text || ',' || document_count::text || ','
           || mention_count::text || ',' || replace(source_roles::text,', ',',') || ','
           || replace(document_languages::text,', ',',') || ']', 'UTF8')), 'hex') AS source_fingerprint
FROM languages
