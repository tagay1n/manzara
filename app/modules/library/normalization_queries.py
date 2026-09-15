"""Catalog mention queries, alias snapshots, and document evidence."""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import text

from app.modules.library.normalization_rules import (
    _entity_config,
    _normalize_text,
)
from app.modules.library.stats import create_runtime_engine, dispose_runtime_engine


def _mentions_cte_sql(entity_type: str) -> str:
    cfg = _entity_config(entity_type)
    schema_field = cfg["schema_field"]
    keys = [str(item) for item in cfg["name_keys"] if str(item)]
    coalesce_parts = [f"e.entity_item->>'{key}'" for key in keys]
    object_name_expr = "COALESCE(" + ", ".join(coalesce_parts + ["''"]) + ")"
    marker_regex = cfg["marker_regex"]

    return f"""
        WITH extracted AS (
            SELECT
                m.md5 AS md5,
                BTRIM(
                    CASE
                        WHEN jsonb_typeof(e.entity_item) = 'object' THEN {object_name_expr}
                        WHEN jsonb_typeof(e.entity_item) = 'string' THEN REGEXP_REPLACE(e.entity_item::text, '^"|"$', '', 'g')
                        ELSE ''
                    END
                ) AS raw_name
            FROM metadata m
            CROSS JOIN LATERAL (
                SELECT elem AS entity_item
                FROM jsonb_array_elements(
                    CASE
                        WHEN jsonb_typeof((m.schema_org::jsonb)->'{schema_field}') = 'array'
                            THEN (m.schema_org::jsonb)->'{schema_field}'
                        ELSE '[]'::jsonb
                    END
                ) AS elem
                UNION ALL
                SELECT (m.schema_org::jsonb)->'{schema_field}' AS entity_item
                WHERE jsonb_typeof((m.schema_org::jsonb)->'{schema_field}') IN ('object', 'string')
            ) e
            WHERE m.lib IS TRUE
              AND m.schema_org IS NOT NULL
        ),
        mentions AS (
            SELECT
                md5,
                raw_name,
                BTRIM(
                    REGEXP_REPLACE(
                        LOWER(
                            REGEXP_REPLACE(
                                raw_name,
                                '[^0-9A-Za-zА-Яа-яЁёӘәҖҗҢңӨөҮүҺһІіҒғҚқҪҫ]+',
                                ' ',
                                'g'
                            )
                        ),
                        '\\s+',
                        ' ',
                        'g'
                    )
                ) AS normalized_name,
                CASE
                    WHEN raw_name ~ '[A-Za-z]' AND raw_name ~ '[А-Яа-яЁёӘәҖҗҢңӨөҮүҺһІіҒғҚқҪҫ]' THEN 'mixed'
                    WHEN raw_name ~ '[А-Яа-яЁёӘәҖҗҢңӨөҮүҺһІіҒғҚқҪҫ]' THEN 'cyrillic'
                    WHEN raw_name ~ '[A-Za-z]' THEN 'latin'
                    ELSE 'other'
                END AS script_label,
                (raw_name ~* '{marker_regex}') AS has_marker
            FROM extracted
            WHERE raw_name <> ''
        )
    """


def _query_aggregated_mentions(
    entity_type: str,
    *,
    search: str = "",
    script_label: str = "",
    min_docs: int = 0,
    limit: int = 5000,
) -> tuple[list[Dict[str, Any]], str]:
    min_docs = max(0, int(min_docs))
    limit = max(1, min(20000, int(limit)))
    search_clean = str(search or "").strip().lower()
    script_clean = str(script_label or "").strip().lower()
    params = {
        "search": search_clean,
        "search_like": f"%{search_clean}%",
        "script_label": script_clean,
        "min_docs": min_docs,
        "limit": limit,
    }

    engine, config_source = create_runtime_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                {_mentions_cte_sql(entity_type)}
                , aggregated AS (
                    SELECT
                        raw_name,
                        normalized_name,
                        script_label,
                        COUNT(*) AS mentions_count,
                        COUNT(DISTINCT md5) AS docs_count,
                        SUM(CASE WHEN has_marker THEN 1 ELSE 0 END) AS marker_count
                    FROM mentions
                    WHERE (:search = '' OR LOWER(raw_name) LIKE :search_like OR normalized_name LIKE :search_like)
                      AND (:script_label = '' OR script_label = :script_label)
                    GROUP BY raw_name, normalized_name, script_label
                    HAVING COUNT(DISTINCT md5) >= :min_docs
                )
                SELECT
                    raw_name,
                    normalized_name,
                    script_label,
                    mentions_count,
                    docs_count,
                    marker_count
                FROM aggregated
                ORDER BY docs_count DESC, mentions_count DESC, raw_name ASC
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
    dispose_runtime_engine(engine)

    return [dict(row) for row in rows], str(config_source)


def _query_docs_with_entities_count(entity_type: str) -> int:
    engine, _ = create_runtime_engine()
    with engine.connect() as conn:
        value = conn.execute(
            text(
                f"""
                {_mentions_cte_sql(entity_type)}
                SELECT COUNT(DISTINCT md5) AS count
                FROM mentions
                """
            )
        ).scalar()
    dispose_runtime_engine(engine)
    return int(value or 0)


def _runtime_snapshot_for_alias(entity_type: str, raw_name: str) -> Dict[str, Any]:
    rows, _ = _query_aggregated_mentions(
        entity_type,
        search=raw_name,
        limit=500,
    )
    for row in rows:
        if str(row.get("raw_name") or "") == raw_name:
            return row
    return {
        "raw_name": raw_name,
        "normalized_name": _normalize_text(raw_name),
        "script_label": "other",
        "mentions_count": 0,
        "docs_count": 0,
        "marker_count": 0,
    }


def get_evidence(
    entity_type: str,
    *,
    raw_name: str,
    limit: int = 20,
) -> Dict[str, Any]:
    """Return sample documents where a raw alias appears."""
    _entity_config(entity_type)
    raw = str(raw_name or "").strip()
    if not raw:
        raise ValueError("raw_name is required")

    limit = max(1, min(200, int(limit)))
    try:
        engine, config_source = create_runtime_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    {_mentions_cte_sql(entity_type)}
                    SELECT
                        m.md5,
                        d.language,
                        d.ya_path,
                        d.mime_type,
                        d.document_url,
                        d.content_url
                    FROM mentions m
                    JOIN document d ON d.md5 = m.md5
                    WHERE m.raw_name = :raw_name
                    ORDER BY d.md5 ASC
                    LIMIT :limit
                    """
                ),
                {"raw_name": raw, "limit": limit},
            ).mappings().all()
        dispose_runtime_engine(engine)
        return {
            "available": True,
            "error": None,
            "config_source": config_source,
            "raw_name": raw,
            "items": [dict(row) for row in rows],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "config_source": None,
            "raw_name": raw,
            "items": [],
        }
