"""Library classification insights and drill-down queries."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List

from sqlalchemy import text

from app.modules.library.response_envelope import available_payload, unavailable_payload
from app.modules.library.stats import create_runtime_engine
from app.modules.library.metadata_terms import defined_term, termset_name

_DEFAULT_PAGE_SIZE = 25
_MAX_PAGE_SIZE = 100
_DEFAULT_ALL_ROWS_LIMIT = 5000
_DEFAULT_DROP_SEGMENTS = [
    "turkic literature",
    "torkic literature",
    "turkic",
]
_DDC_TERMSET = "DDC"
_CATEGORY_PATH_TERMSET = "CategoryPath"
_MANAGED_CLASSIFICATION_TERMSETS = {
    _DDC_TERMSET.casefold(),
    _CATEGORY_PATH_TERMSET.casefold(),
}


def _parse_json_path(path_value: Any) -> list[str]:
    if isinstance(path_value, list):
        return [str(item).strip() for item in path_value if str(item).strip()]
    if isinstance(path_value, str):
        raw = path_value.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return [raw]
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [raw]
    return []


def _format_path(path_value: Any) -> str:
    parts = _parse_json_path(path_value)
    return " / ".join(parts) if parts else "-"


def _digits_prefix(value: str) -> str:
    chars: list[str] = []
    for char in str(value or ""):
        if char.isdigit():
            chars.append(char)
        else:
            break
    return "".join(chars)


def _serialize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _build_defined_term(term_code: str, termset: str) -> dict[str, Any]:
    return defined_term(term_code, termset)


def _coerce_schema_object(schema_org: Any) -> dict[str, Any] | None:
    if isinstance(schema_org, dict):
        return dict(schema_org)
    if isinstance(schema_org, str):
        raw = schema_org.strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return dict(parsed)
    return None


def _rewrite_schema_org_classification_terms(
    schema_org: Any,
    *,
    target_ddc: str,
    target_path_parts: list[str],
) -> tuple[Any, bool]:
    schema = _coerce_schema_object(schema_org)
    if schema is None:
        return schema_org, False

    about_raw = schema.get("about")
    if isinstance(about_raw, list):
        about_items = list(about_raw)
    elif about_raw is None:
        about_items = []
    else:
        about_items = [about_raw]

    retained: list[Any] = []
    for item in about_items:
        if not isinstance(item, dict):
            retained.append(item)
            continue
        termset = str(termset_name(item.get("inDefinedTermSet")) or "").casefold()
        if termset in _MANAGED_CLASSIFICATION_TERMSETS:
            continue
        retained.append(item)

    retained.append(_build_defined_term(target_ddc, _DDC_TERMSET))
    if target_path_parts:
        retained.append(
            _build_defined_term(" > ".join(target_path_parts), _CATEGORY_PATH_TERMSET)
        )

    updated = dict(schema)
    if retained:
        updated["about"] = retained
    else:
        updated.pop("about", None)

    before = json.dumps(schema, ensure_ascii=False, sort_keys=True)
    after = json.dumps(updated, ensure_ascii=False, sort_keys=True)
    return updated, before != after


def _normalize_sort(sort: str) -> str:
    mapping = {
        "usage_desc": "usage_count DESC, c.ddc ASC",
        "usage_asc": "usage_count ASC, c.ddc ASC",
        "ddc_asc": "c.ddc ASC, usage_count DESC",
        "ddc_desc": "c.ddc DESC, usage_count DESC",
        "created_desc": "c.created_at DESC NULLS LAST, usage_count DESC",
        "created_asc": "c.created_at ASC NULLS LAST, usage_count DESC",
    }
    return mapping.get(sort, mapping["usage_desc"])


def _classification_base_sql() -> str:
    return """
        WITH usage AS (
            SELECT m.classification_id, COUNT(m.md5) AS usage_count
            FROM metadata m
            WHERE m.classification_id IS NOT NULL
            GROUP BY m.classification_id
        )
        SELECT
            c.id,
            c.ddc,
            c.path_en,
            c.status,
            c.created_by,
            c.created_at,
            COALESCE(u.usage_count, 0) AS usage_count
        FROM classification c
        LEFT JOIN usage u ON u.classification_id = c.id
        WHERE (:search = '' OR LOWER(c.ddc) LIKE :search_like OR LOWER(CAST(c.path_en AS TEXT)) LIKE :search_like)
          AND (:status = '' OR c.status = :status)
          AND (:ddc_prefix = '' OR c.ddc LIKE :ddc_prefix_like)
          AND COALESCE(u.usage_count, 0) >= :min_usage
    """


def _row_to_classification_item(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "classification_id": int(row.get("id") or 0),
        "ddc": str(row.get("ddc") or ""),
        "path": _format_path(row.get("path_en")),
        "status": str(row.get("status") or ""),
        "created_by": str(row.get("created_by") or ""),
        "created_at": _serialize_value(row.get("created_at")),
        "usage_count": int(row.get("usage_count") or 0),
    }


def list_classifications(
    *,
    search: str = "",
    status: str = "",
    ddc_prefix: str = "",
    min_usage: int = 0,
    page: int = 1,
    page_size: int = _DEFAULT_PAGE_SIZE,
    sort: str = "usage_desc",
) -> Dict[str, Any]:
    """Return paginated classification table with filtering."""
    try:
        page = max(1, int(page))
        page_size = max(1, min(_MAX_PAGE_SIZE, int(page_size)))
        min_usage = max(0, int(min_usage))
        search = str(search or "").strip().lower()
        status = str(status or "").strip()
        ddc_prefix = str(ddc_prefix or "").strip()
        offset = (page - 1) * page_size

        params = {
            "search": search,
            "search_like": f"%{search}%",
            "status": status,
            "ddc_prefix": ddc_prefix,
            "ddc_prefix_like": f"{ddc_prefix}%",
            "min_usage": min_usage,
        }

        engine, config_source = create_runtime_engine()
        with engine.connect() as conn:
            total = int(
                conn.execute(
                    text(f"SELECT COUNT(*) AS count FROM ({_classification_base_sql()}) q"),
                    params,
                ).scalar()
                or 0
            )
            rows = conn.execute(
                text(
                    f"""
                    {_classification_base_sql()}
                    ORDER BY {_normalize_sort(sort)}
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {**params, "limit": page_size, "offset": offset},
            ).mappings().all()
        engine.dispose()

        items = [_row_to_classification_item(dict(row)) for row in rows]
        return available_payload(
            config_source=config_source,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=max(1, (total + page_size - 1) // page_size),
            items=items,
        )
    except Exception as exc:  # noqa: BLE001
        return unavailable_payload(
            exc,
            page=1,
            page_size=page_size,
            total=0,
            total_pages=1,
            items=[],
        )


def _all_classification_usage_rows(limit: int = _DEFAULT_ALL_ROWS_LIMIT) -> tuple[list[dict[str, Any]], str]:
    engine, config_source = create_runtime_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                WITH usage AS (
                    SELECT m.classification_id, COUNT(m.md5) AS usage_count
                    FROM metadata m
                    WHERE m.classification_id IS NOT NULL
                    GROUP BY m.classification_id
                )
                SELECT
                    c.id,
                    c.ddc,
                    c.path_en,
                    c.status,
                    c.created_by,
                    c.created_at,
                    COALESCE(u.usage_count, 0) AS usage_count
                FROM classification c
                LEFT JOIN usage u ON u.classification_id = c.id
                ORDER BY COALESCE(u.usage_count, 0) DESC, c.ddc ASC
                LIMIT :limit
                """
            ),
            {"limit": max(1, int(limit))},
        ).mappings().all()
    engine.dispose()
    return [dict(row) for row in rows], config_source


def _build_tree(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    root: Dict[str, Any] = {"children": {}, "usage_count": 0}
    for row in rows:
        usage = int(row.get("usage_count") or 0)
        if usage <= 0:
            continue
        parts = _parse_json_path(row.get("path_en"))
        if not parts:
            parts = ["Uncategorized"]
        node = root
        node["usage_count"] += usage
        for part in parts:
            children = node["children"]
            if part not in children:
                children[part] = {"name": part, "usage_count": 0, "children": {}}
            node = children[part]
            node["usage_count"] += usage

    def _to_array(node: Dict[str, Any]) -> List[Dict[str, Any]]:
        children = list(node.get("children", {}).values())
        children.sort(key=lambda item: (-int(item.get("usage_count") or 0), str(item.get("name") or "")))
        result: List[Dict[str, Any]] = []
        for child in children:
            result.append(
                {
                    "name": str(child.get("name") or ""),
                    "usage_count": int(child.get("usage_count") or 0),
                    "children": _to_array(child),
                }
            )
        return result

    return _to_array(root)


def _build_distribution(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, int] = defaultdict(int)
    for row in rows:
        usage = int(row.get("usage_count") or 0)
        if usage <= 0:
            continue
        ddc = str(row.get("ddc") or "")
        digits = _digits_prefix(ddc)
        if digits:
            bucket = f"{digits[0]}00"
        else:
            bucket = "other"
        buckets[bucket] += usage

    items = [{"bucket": key, "usage_count": value} for key, value in buckets.items()]
    items.sort(key=lambda item: (item["bucket"] == "other", item["bucket"]))
    total = sum(item["usage_count"] for item in items)
    for item in items:
        item["share_pct"] = round((item["usage_count"] / total) * 100.0, 2) if total else 0.0
    return items


def _build_duplicates(rows: Iterable[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        path_parts = _parse_json_path(row.get("path_en"))
        if not path_parts:
            continue
        key = " / ".join(part.strip().lower() for part in path_parts)
        groups[key].append(row)

    results: List[Dict[str, Any]] = []
    for key, group_rows in groups.items():
        if len(group_rows) <= 1:
            continue
        distinct_ddc = {str(item.get("ddc") or "").strip() for item in group_rows}
        total_usage = sum(int(item.get("usage_count") or 0) for item in group_rows)
        sample_path = _format_path(group_rows[0].get("path_en"))
        items = sorted(
            [
                {
                    "classification_id": int(item.get("id") or 0),
                    "ddc": str(item.get("ddc") or ""),
                    "status": str(item.get("status") or ""),
                    "usage_count": int(item.get("usage_count") or 0),
                }
                for item in group_rows
            ],
            key=lambda item: (-item["usage_count"], item["ddc"]),
        )
        results.append(
            {
                "path_key": key,
                "path": sample_path,
                "issue": "ddc_conflict" if len(distinct_ddc) > 1 else "duplicate_path",
                "total_usage": total_usage,
                "distinct_ddc_count": len(distinct_ddc),
                "items": items,
            }
        )

    results.sort(key=lambda item: (-int(item["total_usage"]), str(item["path"])))
    return results[: max(1, int(limit))]


def _fetch_unclassified_applicable(limit: int) -> Dict[str, Any]:
    engine, _config_source = create_runtime_engine()
    with engine.connect() as conn:
        total = int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*) AS count
                    FROM metadata m
                    JOIN document d ON d.md5 = m.md5
                    WHERE m.lib IS TRUE
                      AND m.classification_id IS NULL
                    """
                )
            ).scalar()
            or 0
        )
        rows = conn.execute(
            text(
                """
                SELECT
                    d.md5,
                    d.language,
                    d.ya_path,
                    d.mime_type,
                    d.document_url
                FROM metadata m
                JOIN document d ON d.md5 = m.md5
                WHERE m.lib IS TRUE
                  AND m.classification_id IS NULL
                ORDER BY d.md5 ASC
                LIMIT :limit
                """
            ),
            {"limit": max(1, int(limit))},
        ).mappings().all()
    engine.dispose()
    return {
        "total": total,
        "items": [
            {
                "md5": str(row.get("md5") or ""),
                "language": str(row.get("language") or ""),
                "ya_path": str(row.get("ya_path") or ""),
                "mime_type": str(row.get("mime_type") or ""),
                "document_url": str(row.get("document_url") or ""),
            }
            for row in rows
        ],
    }


def get_classification_insights(
    *,
    row_limit: int = _DEFAULT_ALL_ROWS_LIMIT,
    duplicate_limit: int = 25,
    unclassified_limit: int = 30,
) -> Dict[str, Any]:
    """Return hierarchy, DDC distribution, duplicate path clusters and queue."""
    try:
        rows, config_source = _all_classification_usage_rows(limit=row_limit)
        return available_payload(
            config_source=config_source,
            tree=_build_tree(rows),
            distribution=_build_distribution(rows),
            duplicates=_build_duplicates(rows, limit=duplicate_limit),
            unclassified_queue=_fetch_unclassified_applicable(unclassified_limit),
        )
    except Exception as exc:  # noqa: BLE001
        return unavailable_payload(
            exc,
            tree=[],
            distribution=[],
            duplicates=[],
            unclassified_queue={"total": 0, "items": []},
        )




__all__ = ["list_classifications", "get_classification_insights"]
