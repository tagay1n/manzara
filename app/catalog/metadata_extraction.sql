WITH candidates AS MATERIALIZED (
    {{publications}}
), eligible_sources AS MATERIALIZED (
    {{sources}}
    AND d.publication_id IN (SELECT publication_id FROM candidates)
)
SELECT p.*, source_batch.sources
FROM candidates p
LEFT JOIN catalog_classifications classification ON classification.classification_id=p.classification_id
LEFT JOIN LATERAL (
    SELECT jsonb_agg(language ORDER BY position) AS value
    FROM catalog_publication_languages WHERE publication_id=p.publication_id
) languages ON TRUE
LEFT JOIN LATERAL (
    SELECT jsonb_agg(to_jsonb(credit) || jsonb_build_object(
        'role_name', groups.role_name, 'kind', names.kind,
        'raw_name', names.raw_name, 'display_name', entities.display_name)
        ORDER BY credit.role, credit.position, credit.nested_position) AS value
    FROM catalog_contributions credit
    JOIN catalog_credit_groups groups ON groups.publication_id=credit.publication_id
        AND groups.role=credit.role AND groups.position=credit.position
    JOIN catalog_names names ON names.name_id=credit.name_id
    LEFT JOIN catalog_entities entities ON entities.entity_id=credit.entity_id
        AND entities.status='active' AND entities.approval='confirmed' AND credit.resolution='confirmed'
    WHERE credit.publication_id=p.publication_id
) credits ON TRUE
LEFT JOIN LATERAL (
    SELECT jsonb_agg(value ORDER BY position) AS value
    FROM catalog_identifiers WHERE publication_id=p.publication_id AND kind='isbn'
) identifiers ON TRUE
LEFT JOIN LATERAL (
    SELECT jsonb_agg(value ORDER BY position) AS value
    FROM catalog_genres WHERE publication_id=p.publication_id
) genres ON TRUE
LEFT JOIN LATERAL (
    SELECT jsonb_agg(
        jsonb_build_object('@type', 'DefinedTerm', 'inDefinedTermSet',
            CASE WHEN set_is_url THEN to_jsonb(set_url)
            ELSE jsonb_build_object('@type', 'DefinedTermSet', 'name', set_name)
                || CASE WHEN set_url IS NOT NULL THEN jsonb_build_object('url', set_url) ELSE '{}'::jsonb END END)
        || CASE WHEN name IS NOT NULL THEN jsonb_build_object('name', name) ELSE '{}'::jsonb END
        || CASE WHEN term_code IS NOT NULL THEN jsonb_build_object('termCode', term_code) ELSE '{}'::jsonb END
        ORDER BY position) AS value
    FROM catalog_subjects WHERE publication_id=p.publication_id
) subjects ON TRUE
LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_object('@type', kind) || jsonb_strip_nulls(jsonb_build_object(
        'audienceType', audience_type, 'suggestedMinAge', min_age, 'suggestedMaxAge', max_age))
        ORDER BY position) AS value
    FROM catalog_audiences WHERE publication_id=p.publication_id
) audiences ON TRUE
LEFT JOIN LATERAL (
    SELECT jsonb_strip_nulls(jsonb_build_object('@type', reference.work_type,
        'name', reference.name, 'inLanguage', reference.language))
        || CASE WHEN reference.urls_present THEN jsonb_build_object('url', COALESCE((
            SELECT jsonb_agg(url ORDER BY position) FROM catalog_reference_urls
            WHERE publication_id=p.publication_id), '[]'::jsonb)) ELSE '{}'::jsonb END
        || CASE WHEN authors.value IS NOT NULL THEN jsonb_build_object('author', authors.value)
            ELSE '{}'::jsonb END AS value
    FROM catalog_references reference
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object('@type', kind, 'name', name) ORDER BY position) AS value
        FROM catalog_reference_authors WHERE publication_id=p.publication_id
    ) authors ON TRUE
    WHERE reference.publication_id=p.publication_id
) reference_metadata ON TRUE
JOIN LATERAL (
    SELECT jsonb_agg(to_jsonb(source) || jsonb_build_object(
        'metadata_record', jsonb_build_object(
            'publication_id', p.publication_id, 'revision', p.revision,
            'scalars', jsonb_strip_nulls(jsonb_build_object(
                'work_type', p.work_type, 'name', p.name, 'description', p.description,
                'edition', p.edition, 'date_published', p.date_published, 'page_count', p.page_count)),
            'languages', COALESCE(languages.value, '[]'::jsonb),
            'credits', COALESCE(credits.value, '[]'::jsonb),
            'identifiers', COALESCE(identifiers.value, '[]'::jsonb),
            'genres', COALESCE(genres.value, '[]'::jsonb),
            'subjects', COALESCE(subjects.value, '[]'::jsonb),
            'audiences', COALESCE(audiences.value, '[]'::jsonb), 'audience_array', p.audience_array,
            'access_modes', COALESCE(access_modes.value, '[]'::jsonb),
            'sufficient_modes', COALESCE(sufficient_modes.value, '[]'::jsonb),
            'based_on', reference_metadata.value),
        'classification_path', CASE WHEN classification.node_id IS NOT NULL
            THEN catalog_path(classification.node_id) END)
        ORDER BY source.selected DESC, source.md5) AS sources
    FROM eligible_sources source
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(mode ORDER BY position) AS value
        FROM catalog_document_access_modes WHERE md5=source.md5
    ) access_modes ON TRUE
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(COALESCE(items.value, '[]'::jsonb) ORDER BY groups.position) AS value
        FROM catalog_sufficient_modes groups
        LEFT JOIN LATERAL (
            SELECT jsonb_agg(mode ORDER BY position) AS value
            FROM catalog_sufficient_mode_items WHERE md5=source.md5 AND group_position=groups.position
        ) items ON TRUE
        WHERE groups.md5=source.md5
    ) sufficient_modes ON TRUE
    WHERE source.publication_id=p.publication_id
) source_batch ON TRUE
ORDER BY p.publication_id
