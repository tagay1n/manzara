"""Library personality (author) insights."""

from __future__ import annotations

from functools import partial

from app.modules.library.entity_insights import (
    EntityInsightsConfig,
    get_entity_insights,
    get_entity_overview,
    list_entities,
)

_CONFIG = EntityInsightsConfig(
    mentions_cte_sql="""
        WITH entity_mentions AS (
            SELECT m.md5,
                   BTRIM(CASE
                     WHEN jsonb_typeof(a.item) = 'object'
                       THEN COALESCE(a.item->>'name', a.item->>'alternateName', '')
                     WHEN jsonb_typeof(a.item) = 'string'
                       THEN REGEXP_REPLACE(a.item::text, '^"|"$', '', 'g')
                     ELSE '' END) AS raw_name
            FROM metadata m
            CROSS JOIN LATERAL (
                SELECT elem AS item
                FROM jsonb_array_elements(CASE
                    WHEN jsonb_typeof((m.schema_org::jsonb)->'author') = 'array'
                    THEN (m.schema_org::jsonb)->'author' ELSE '[]'::jsonb END) AS elem
                UNION ALL
                SELECT (m.schema_org::jsonb)->'author' AS item
                WHERE jsonb_typeof((m.schema_org::jsonb)->'author') IN ('object', 'string')
            ) a
            WHERE m.lib IS TRUE AND m.schema_org IS NOT NULL
        ),
        mentions AS (
            SELECT md5, raw_name,
                   BTRIM(REGEXP_REPLACE(LOWER(REGEXP_REPLACE(
                     raw_name, '[^0-9A-Za-zА-Яа-яЁёӘәҖҗҢңӨөҮүҺһІіҒғҚқҪҫ]+', ' ', 'g'
                   )), '\\s+', ' ', 'g')) AS normalized_name,
                   CASE
                     WHEN raw_name ~ '[A-Za-z]' AND raw_name ~ '[А-Яа-яЁёӘәҖҗҢңӨөҮүҺһІіҒғҚқҪҫ]' THEN 'mixed'
                     WHEN raw_name ~ '[А-Яа-яЁёӘәҖҗҢңӨөҮүҺһІіҒғҚқҪҫ]' THEN 'cyrillic'
                     WHEN raw_name ~ '[A-Za-z]' THEN 'latin' ELSE 'other' END AS script_label,
                   (raw_name ~* '(^|\\s)(улы|кызы|оглы|оглу|ович|евич|овна|евна)(\\s|$)') AS has_marker
            FROM entity_mentions WHERE raw_name <> ''
        )
    """,
    docs_stat_key="docs_with_authors",
    marker_key="patronymic_mentions",
    marker_reason="patronymic_form",
    top_key="top_personalities",
)

get_personality_overview = partial(get_entity_overview, _CONFIG)
list_personalities = partial(list_entities, _CONFIG)
get_personality_insights = partial(get_entity_insights, _CONFIG)

__all__ = ["get_personality_overview", "list_personalities", "get_personality_insights"]
