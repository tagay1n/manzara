WITH batch AS MATERIALIZED (
    {{envelope}}
), known_classifications AS MATERIALIZED (
    SELECT COALESCE(jsonb_agg(jsonb_build_object('id', known.id, 'ddc', known.ddc, 'path', known.path)
        ORDER BY known.usage_count DESC, known.ddc, known.id), '[]'::jsonb) AS value
    FROM (
        SELECT c.classification_id AS id, catalog_path(c.node_id)->>'ddc' AS ddc,
               catalog_path(c.node_id)->'path_en' AS path, COUNT(p.publication_id) AS usage_count
        FROM catalog_classifications c LEFT JOIN catalog_publications p USING(classification_id)
        WHERE EXISTS (SELECT 1 FROM batch)
        GROUP BY c.classification_id, c.node_id
        ORDER BY usage_count DESC, ddc, c.classification_id LIMIT :known_classification_limit
    ) known
)
SELECT COALESCE((SELECT jsonb_agg(to_jsonb(batch) ORDER BY publication_id) FROM batch), '[]'::jsonb)
           AS publications,
       known_classifications.value AS known_classifications
FROM known_classifications
