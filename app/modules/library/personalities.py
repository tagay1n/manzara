"""Library personality (author) insights sourced from metadata schema.org payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from sqlalchemy import text

from app.modules.library.response_envelope import available_payload, unavailable_payload
from app.modules.library.stats import create_runtime_engine

_DEFAULT_PAGE_SIZE = 25
_MAX_PAGE_SIZE = 100


def _mentions_cte_sql() -> str:
    return """
        WITH author_mentions AS (
            SELECT
                m.md5 AS md5,
                BTRIM(
                    CASE
                        WHEN jsonb_typeof(a.author_item) = 'object' THEN COALESCE(a.author_item->>'name', a.author_item->>'alternateName', '')
                        WHEN jsonb_typeof(a.author_item) = 'string' THEN REGEXP_REPLACE(a.author_item::text, '^"|"$', '', 'g')
                        ELSE ''
                    END
                ) AS raw_name
            FROM metadata m
            CROSS JOIN LATERAL (
                SELECT elem AS author_item
                FROM jsonb_array_elements(
                    CASE
                        WHEN jsonb_typeof((m.schema_org::jsonb)->'author') = 'array'
                            THEN (m.schema_org::jsonb)->'author'
                        ELSE '[]'::jsonb
                    END
                ) AS elem
                UNION ALL
                SELECT (m.schema_org::jsonb)->'author' AS author_item
                WHERE jsonb_typeof((m.schema_org::jsonb)->'author') IN ('object', 'string')
            ) a
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
                (raw_name ~* '(^|\\s)(улы|кызы|оглы|оглу|ович|евич|овна|евна)(\\s|$)') AS has_patronymic
            FROM author_mentions
            WHERE raw_name <> ''
        )
    """


def _normalize_sort(sort: str) -> str:
    mapping = {
        "docs_desc": "docs_count DESC, mentions_count DESC, raw_name ASC",
        "docs_asc": "docs_count ASC, mentions_count DESC, raw_name ASC",
        "mentions_desc": "mentions_count DESC, docs_count DESC, raw_name ASC",
        "mentions_asc": "mentions_count ASC, docs_count DESC, raw_name ASC",
        "name_asc": "raw_name ASC",
        "name_desc": "raw_name DESC",
    }
    return mapping.get(sort, mapping["docs_desc"])


def _row_to_personality_item(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "raw_name": str(row.get("raw_name") or ""),
        "normalized_name": str(row.get("normalized_name") or ""),
        "script_label": str(row.get("script_label") or "other"),
        "docs_count": int(row.get("docs_count") or 0),
        "mentions_count": int(row.get("mentions_count") or 0),
        "patronymic_mentions": int(row.get("patronymic_mentions") or 0),
    }


def get_personality_overview(top_limit: int = 12) -> Dict[str, Any]:
    """Return high-level author/personality statistics and top names."""
    try:
        engine, config_source = create_runtime_engine()
        with engine.connect() as conn:
            stats_row = conn.execute(
                text(
                    f"""
                    {_mentions_cte_sql()}
                    SELECT
                        COUNT(*) AS total_mentions,
                        COUNT(DISTINCT md5) AS docs_with_authors,
                        COUNT(DISTINCT raw_name) AS unique_raw_names,
                        COUNT(DISTINCT normalized_name) AS unique_normalized_names,
                        SUM(CASE WHEN script_label = 'mixed' THEN 1 ELSE 0 END) AS mixed_script_mentions,
                        SUM(CASE WHEN has_patronymic THEN 1 ELSE 0 END) AS patronymic_mentions
                    FROM mentions
                    """
                )
            ).mappings().first() or {}

            top_rows = conn.execute(
                text(
                    f"""
                    {_mentions_cte_sql()}
                    SELECT
                        raw_name,
                        normalized_name,
                        script_label,
                        COUNT(*) AS mentions_count,
                        COUNT(DISTINCT md5) AS docs_count,
                        SUM(CASE WHEN has_patronymic THEN 1 ELSE 0 END) AS patronymic_mentions
                    FROM mentions
                    GROUP BY raw_name, normalized_name, script_label
                    ORDER BY docs_count DESC, mentions_count DESC, raw_name ASC
                    LIMIT :limit
                    """
                ),
                {"limit": max(1, min(int(top_limit), 50))},
            ).mappings().all()
        engine.dispose()

        return available_payload(
            config_source=config_source,
            generated_at=datetime.utcnow().isoformat() + "Z",
            stats={
                "total_mentions": int(stats_row.get("total_mentions") or 0),
                "docs_with_authors": int(stats_row.get("docs_with_authors") or 0),
                "unique_raw_names": int(stats_row.get("unique_raw_names") or 0),
                "unique_normalized_names": int(stats_row.get("unique_normalized_names") or 0),
                "mixed_script_mentions": int(stats_row.get("mixed_script_mentions") or 0),
                "patronymic_mentions": int(stats_row.get("patronymic_mentions") or 0),
            },
            top_personalities=[_row_to_personality_item(row) for row in top_rows],
        )
    except Exception as exc:  # noqa: BLE001
        return unavailable_payload(
            exc,
            generated_at=datetime.utcnow().isoformat() + "Z",
            stats={
                "total_mentions": 0,
                "docs_with_authors": 0,
                "unique_raw_names": 0,
                "unique_normalized_names": 0,
                "mixed_script_mentions": 0,
                "patronymic_mentions": 0,
            },
            top_personalities=[],
        )


def list_personalities(
    *,
    search: str = "",
    script_label: str = "",
    min_docs: int = 0,
    page: int = 1,
    page_size: int = _DEFAULT_PAGE_SIZE,
    sort: str = "docs_desc",
) -> Dict[str, Any]:
    """Return paginated table of extracted personality names."""
    page = max(1, int(page))
    page_size = max(1, min(_MAX_PAGE_SIZE, int(page_size)))
    min_docs = max(0, int(min_docs))
    offset = (page - 1) * page_size

    params = {
        "search": (search or "").strip().lower(),
        "search_like": f"%{(search or '').strip().lower()}%",
        "script_label": (script_label or "").strip().lower(),
        "min_docs": min_docs,
        "limit": page_size,
        "offset": offset,
    }

    order_sql = _normalize_sort(sort)

    try:
        engine, config_source = create_runtime_engine()
        with engine.connect() as conn:
            total = int(
                conn.execute(
                    text(
                        f"""
                        {_mentions_cte_sql()}
                        , aggregated AS (
                            SELECT
                                raw_name,
                                normalized_name,
                                script_label,
                                COUNT(*) AS mentions_count,
                                COUNT(DISTINCT md5) AS docs_count,
                                SUM(CASE WHEN has_patronymic THEN 1 ELSE 0 END) AS patronymic_mentions
                            FROM mentions
                            WHERE (:search = '' OR LOWER(raw_name) LIKE :search_like OR normalized_name LIKE :search_like)
                              AND (:script_label = '' OR script_label = :script_label)
                            GROUP BY raw_name, normalized_name, script_label
                            HAVING COUNT(DISTINCT md5) >= :min_docs
                        )
                        SELECT COUNT(*) AS count
                        FROM aggregated
                        """
                    ),
                    params,
                ).scalar()
                or 0
            )

            rows = conn.execute(
                text(
                    f"""
                    {_mentions_cte_sql()}
                    , aggregated AS (
                        SELECT
                            raw_name,
                            normalized_name,
                            script_label,
                            COUNT(*) AS mentions_count,
                            COUNT(DISTINCT md5) AS docs_count,
                            SUM(CASE WHEN has_patronymic THEN 1 ELSE 0 END) AS patronymic_mentions
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
                        patronymic_mentions
                    FROM aggregated
                    ORDER BY {order_sql}
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            ).mappings().all()
        engine.dispose()

        total_pages = max(1, (total + page_size - 1) // page_size)
        return available_payload(
            config_source=config_source,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            items=[_row_to_personality_item(row) for row in rows],
        )
    except Exception as exc:  # noqa: BLE001
        return unavailable_payload(
            exc,
            page=page,
            page_size=page_size,
            total=0,
            total_pages=1,
            items=[],
        )


def _script_distribution(rows: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    total_mentions = sum(int(row.get("mentions_count") or 0) for row in rows)
    payload: list[Dict[str, Any]] = []
    for row in rows:
        mentions_count = int(row.get("mentions_count") or 0)
        share = 0.0
        if total_mentions > 0:
            share = round((mentions_count / total_mentions) * 100.0, 2)
        payload.append(
            {
                "script_label": str(row.get("script_label") or "other"),
                "mentions_count": mentions_count,
                "docs_count": int(row.get("docs_count") or 0),
                "share_pct": share,
            }
        )
    return payload


def _ambiguous_reasons(row: Dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    raw_name = str(row.get("raw_name") or "")
    script_label = str(row.get("script_label") or "")
    normalized_name = str(row.get("normalized_name") or "")
    if " " not in raw_name.strip():
        reasons.append("single_token")
    if script_label == "mixed":
        reasons.append("mixed_script")
    if int(row.get("patronymic_mentions") or 0) > 0:
        reasons.append("patronymic_form")
    if not normalized_name:
        reasons.append("empty_normalized")
    return reasons or ["manual_review"]


def get_personality_insights(
    *,
    cluster_limit: int = 24,
    queue_limit: int = 40,
) -> Dict[str, Any]:
    """Return script distribution, variant clusters, and ambiguous queue."""
    cluster_limit = max(1, min(int(cluster_limit), 100))
    queue_limit = max(1, min(int(queue_limit), 200))

    try:
        engine, config_source = create_runtime_engine()
        with engine.connect() as conn:
            script_rows = conn.execute(
                text(
                    f"""
                    {_mentions_cte_sql()}
                    SELECT
                        script_label,
                        COUNT(*) AS mentions_count,
                        COUNT(DISTINCT md5) AS docs_count
                    FROM mentions
                    GROUP BY script_label
                    ORDER BY COUNT(*) DESC, script_label ASC
                    """
                )
            ).mappings().all()

            cluster_rows = conn.execute(
                text(
                    f"""
                    {_mentions_cte_sql()}
                    SELECT
                        normalized_name,
                        COUNT(DISTINCT raw_name) AS variants_count,
                        COUNT(*) AS mentions_count,
                        COUNT(DISTINCT md5) AS docs_count
                    FROM mentions
                    WHERE normalized_name <> ''
                    GROUP BY normalized_name
                    HAVING COUNT(DISTINCT raw_name) > 1
                    ORDER BY docs_count DESC, variants_count DESC, normalized_name ASC
                    LIMIT :limit
                    """
                ),
                {"limit": cluster_limit},
            ).mappings().all()

            variant_clusters: list[Dict[str, Any]] = []
            for cluster in cluster_rows:
                normalized_name = str(cluster.get("normalized_name") or "")
                items = conn.execute(
                    text(
                        f"""
                        {_mentions_cte_sql()}
                        SELECT
                            raw_name,
                            script_label,
                            COUNT(*) AS mentions_count,
                            COUNT(DISTINCT md5) AS docs_count,
                            SUM(CASE WHEN has_patronymic THEN 1 ELSE 0 END) AS patronymic_mentions
                        FROM mentions
                        WHERE normalized_name = :normalized_name
                        GROUP BY raw_name, script_label
                        ORDER BY docs_count DESC, mentions_count DESC, raw_name ASC
                        LIMIT 8
                        """
                    ),
                    {"normalized_name": normalized_name},
                ).mappings().all()
                variant_clusters.append(
                    {
                        "normalized_name": normalized_name,
                        "variants_count": int(cluster.get("variants_count") or 0),
                        "mentions_count": int(cluster.get("mentions_count") or 0),
                        "docs_count": int(cluster.get("docs_count") or 0),
                        "variants": [_row_to_personality_item(row) for row in items],
                    }
                )

            queue_rows = conn.execute(
                text(
                    f"""
                    {_mentions_cte_sql()}
                    SELECT
                        raw_name,
                        normalized_name,
                        script_label,
                        COUNT(*) AS mentions_count,
                        COUNT(DISTINCT md5) AS docs_count,
                        SUM(CASE WHEN has_patronymic THEN 1 ELSE 0 END) AS patronymic_mentions
                    FROM mentions
                    GROUP BY raw_name, normalized_name, script_label
                    HAVING COUNT(DISTINCT md5) >= 1
                       AND (
                            POSITION(' ' IN BTRIM(raw_name)) = 0
                            OR script_label = 'mixed'
                            OR SUM(CASE WHEN has_patronymic THEN 1 ELSE 0 END) > 0
                            OR normalized_name = ''
                       )
                    ORDER BY docs_count DESC, mentions_count DESC, raw_name ASC
                    LIMIT :limit
                    """
                ),
                {"limit": queue_limit},
            ).mappings().all()
        engine.dispose()

        queue_items = []
        for row in queue_rows:
            item = _row_to_personality_item(row)
            item["reasons"] = _ambiguous_reasons(row)
            queue_items.append(item)

        return available_payload(
            config_source=config_source,
            script_distribution=_script_distribution(list(script_rows)),
            variant_clusters=variant_clusters,
            summary={
                "script_total_mentions": sum(
                    int(row.get("mentions_count") or 0) for row in script_rows
                ),
                "variant_cluster_count": len(variant_clusters),
                "ambiguous_queue_total": len(queue_items),
            },
            ambiguous_queue={
                "total": len(queue_items),
                "items": queue_items,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return unavailable_payload(
            exc,
            script_distribution=[],
            variant_clusters=[],
            summary={
                "script_total_mentions": 0,
                "variant_cluster_count": 0,
                "ambiguous_queue_total": 0,
            },
            ambiguous_queue={"total": 0, "items": []},
        )


__all__ = [
    "get_personality_overview",
    "list_personalities",
    "get_personality_insights",
]
