"""Library classification insights and drill-down queries."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List

from sqlalchemy import text

from app.modules.library.stats import create_runtime_engine

_DEFAULT_PAGE_SIZE = 25
_MAX_PAGE_SIZE = 100
_DEFAULT_ALL_ROWS_LIMIT = 5000
_DEFAULT_DROP_SEGMENTS = [
    "turkic literature",
    "torkic literature",
    "turkic",
]


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
        return {
            "available": True,
            "error": None,
            "config_source": config_source,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": max(1, (total + page_size - 1) // page_size),
            "items": items,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "config_source": None,
            "page": 1,
            "page_size": page_size,
            "total": 0,
            "total_pages": 1,
            "items": [],
        }


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
        return {
            "available": True,
            "error": None,
            "config_source": config_source,
            "tree": _build_tree(rows),
            "distribution": _build_distribution(rows),
            "duplicates": _build_duplicates(rows, limit=duplicate_limit),
            "unclassified_queue": _fetch_unclassified_applicable(unclassified_limit),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "config_source": None,
            "tree": [],
            "distribution": [],
            "duplicates": [],
            "unclassified_queue": {"total": 0, "items": []},
        }


def _normalize_text(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _drop_segment_set(drop_segments: Iterable[str] | None) -> set[str]:
    source = list(drop_segments or []) or list(_DEFAULT_DROP_SEGMENTS)
    return {_normalize_text(item) for item in source if _normalize_text(item)}


def _normalize_path_parts(parts: list[str], drop_set: set[str]) -> tuple[list[str], list[str]]:
    normalized_parts: list[str] = []
    removed_parts: list[str] = []
    for part in parts:
        key = _normalize_text(part)
        if key in drop_set:
            removed_parts.append(part)
            continue
        normalized_parts.append(part)
    return normalized_parts, removed_parts


def get_normalization_preview(
    *,
    drop_segments: list[str] | None = None,
    limit: int = 120,
    row_limit: int = _DEFAULT_ALL_ROWS_LIMIT,
) -> Dict[str, Any]:
    """Preview classification simplification using drop-segment normalization rules."""
    try:
        rows, config_source = _all_classification_usage_rows(limit=row_limit)
        drop_set = _drop_segment_set(drop_segments)

        affected: list[Dict[str, Any]] = []
        groups: Dict[str, list[Dict[str, Any]]] = defaultdict(list)

        for row in rows:
            original_parts = _parse_json_path(row.get("path_en"))
            normalized_parts, removed_parts = _normalize_path_parts(original_parts, drop_set)
            original_path = " / ".join(original_parts) if original_parts else "-"
            normalized_path = " / ".join(normalized_parts) if normalized_parts else "-"
            usage_count = int(row.get("usage_count") or 0)
            if original_parts != normalized_parts:
                item = {
                    "classification_id": int(row.get("id") or 0),
                    "ddc": str(row.get("ddc") or ""),
                    "original_path": original_path,
                    "normalized_path": normalized_path,
                    "usage_count": usage_count,
                    "removed_segments": removed_parts,
                }
                affected.append(item)
            groups[_normalize_text(normalized_path)].append(
                {
                    "classification_id": int(row.get("id") or 0),
                    "ddc": str(row.get("ddc") or ""),
                    "path": original_path,
                    "normalized_path": normalized_path,
                    "usage_count": usage_count,
                }
            )

        affected.sort(key=lambda item: (-int(item["usage_count"]), item["ddc"], item["classification_id"]))

        merge_groups: list[Dict[str, Any]] = []
        for _key, items in groups.items():
            if len(items) <= 1:
                continue
            sorted_items = sorted(items, key=lambda item: (-int(item["usage_count"]), item["classification_id"]))
            canonical = sorted_items[0]
            total_usage = sum(int(item["usage_count"]) for item in sorted_items)
            merge_groups.append(
                {
                    "normalized_path": sorted_items[0]["normalized_path"],
                    "group_size": len(sorted_items),
                    "total_usage": total_usage,
                    "recommended_primary_classification_id": int(canonical["classification_id"]),
                    "items": sorted_items,
                }
            )

        merge_groups.sort(key=lambda item: (-int(item["total_usage"]), -int(item["group_size"])))

        return {
            "available": True,
            "error": None,
            "config_source": config_source,
            "rules": {
                "drop_segments": sorted(drop_set),
            },
            "summary": {
                "total_rows_scanned": len(rows),
                "affected_classifications": len(affected),
                "estimated_reassigned_documents": sum(int(item["usage_count"]) for item in affected),
                "merge_group_candidates": len(merge_groups),
            },
            "affected_preview": affected[: max(1, int(limit))],
            "merge_groups": merge_groups[: max(1, min(int(limit), 80))],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "config_source": None,
            "rules": {"drop_segments": []},
            "summary": {
                "total_rows_scanned": 0,
                "affected_classifications": 0,
                "estimated_reassigned_documents": 0,
                "merge_group_candidates": 0,
            },
            "affected_preview": [],
            "merge_groups": [],
        }


def _path_tokens(path: str) -> set[str]:
    tokens = re.split(r"[^a-z0-9]+", _normalize_text(path))
    return {token for token in tokens if len(token) > 1}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def get_merge_candidates(
    *,
    limit: int = 80,
    min_score: float = 0.78,
    row_limit: int = 1200,
) -> Dict[str, Any]:
    """Return ranked near-duplicate classification merge candidates."""
    try:
        rows, config_source = _all_classification_usage_rows(limit=row_limit)
        items = []
        for row in rows:
            path = _format_path(row.get("path_en"))
            path_key = _normalize_text(path)
            ddc = str(row.get("ddc") or "")
            ddc_digits = _digits_prefix(ddc)
            usage_count = int(row.get("usage_count") or 0)
            parts = _parse_json_path(row.get("path_en"))
            root = _normalize_text(parts[0]) if parts else ""
            tokens = _path_tokens(path)
            items.append(
                {
                    "classification_id": int(row.get("id") or 0),
                    "ddc": ddc,
                    "usage_count": usage_count,
                    "path": path,
                    "path_key": path_key,
                    "tokens": tokens,
                    "root": root,
                    "ddc1": ddc_digits[:1],
                    "ddc2": ddc_digits[:2],
                }
            )

        items.sort(key=lambda item: (-int(item["usage_count"]), item["classification_id"]))
        max_items = min(len(items), 900)
        items = items[:max_items]

        candidates: list[Dict[str, Any]] = []
        min_score = max(0.0, min(1.0, float(min_score)))

        for i in range(len(items)):
            left = items[i]
            for j in range(i + 1, len(items)):
                right = items[j]

                # Fast block to avoid O(n^2) across unrelated branches.
                same_root = left["root"] and left["root"] == right["root"]
                same_ddc1 = left["ddc1"] and left["ddc1"] == right["ddc1"]
                if not same_root and not same_ddc1:
                    continue

                seq = SequenceMatcher(None, left["path_key"], right["path_key"]).ratio()
                jac = _jaccard(left["tokens"], right["tokens"])
                ddc_bonus = 0.0
                if left["ddc"] and right["ddc"] and left["ddc"] == right["ddc"]:
                    ddc_bonus += 0.08
                elif left["ddc2"] and right["ddc2"] and left["ddc2"] == right["ddc2"]:
                    ddc_bonus += 0.03

                score = 0.55 * seq + 0.35 * jac + ddc_bonus
                issue = "near_duplicate"
                if left["path_key"] == right["path_key"] and left["ddc"] != right["ddc"]:
                    issue = "ddc_conflict"
                    score = max(score, 0.86)
                if score < min_score:
                    continue

                primary = left if int(left["usage_count"]) >= int(right["usage_count"]) else right
                secondary = right if primary is left else left
                impact = int(left["usage_count"]) + int(right["usage_count"])
                candidates.append(
                    {
                        "issue": issue,
                        "score": round(score, 3),
                        "impact": impact,
                        "recommended_primary_classification_id": int(primary["classification_id"]),
                        "primary": {
                            "classification_id": int(primary["classification_id"]),
                            "ddc": str(primary["ddc"]),
                            "path": str(primary["path"]),
                            "usage_count": int(primary["usage_count"]),
                        },
                        "secondary": {
                            "classification_id": int(secondary["classification_id"]),
                            "ddc": str(secondary["ddc"]),
                            "path": str(secondary["path"]),
                            "usage_count": int(secondary["usage_count"]),
                        },
                    }
                )

        candidates.sort(key=lambda item: (-float(item["score"]), -int(item["impact"])))
        return {
            "available": True,
            "error": None,
            "config_source": config_source,
            "summary": {
                "rows_scanned": len(items),
                "candidate_count": len(candidates),
                "min_score": min_score,
            },
            "candidates": candidates[: max(1, int(limit))],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "config_source": None,
            "summary": {
                "rows_scanned": 0,
                "candidate_count": 0,
                "min_score": min_score,
            },
            "candidates": [],
        }


def get_classification_detail(
    classification_id: int,
    *,
    docs_page: int = 1,
    docs_page_size: int = 40,
) -> Dict[str, Any]:
    """Return one classification with linked docs and language split."""
    try:
        classification_id = int(classification_id)
        docs_page = max(1, int(docs_page))
        docs_page_size = max(1, min(200, int(docs_page_size)))
        offset = (docs_page - 1) * docs_page_size

        engine, config_source = create_runtime_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                        c.id,
                        c.ddc,
                        c.path_en,
                        c.path_tt,
                        c.status,
                        c.created_by,
                        c.created_at,
                        COALESCE(usage.usage_count, 0) AS usage_count
                    FROM classification c
                    LEFT JOIN (
                        SELECT classification_id, COUNT(md5) AS usage_count
                        FROM metadata
                        WHERE classification_id IS NOT NULL
                        GROUP BY classification_id
                    ) usage ON usage.classification_id = c.id
                    WHERE c.id = :classification_id
                    """
                ),
                {"classification_id": classification_id},
            ).mappings().first()
            if not row:
                raise ValueError("Classification not found")

            docs_total = int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*) AS count
                        FROM metadata m
                        JOIN document d ON d.md5 = m.md5
                        WHERE m.classification_id = :classification_id
                        """
                    ),
                    {"classification_id": classification_id},
                ).scalar()
                or 0
            )

            docs_rows = conn.execute(
                text(
                    """
                    SELECT
                        d.md5,
                        d.language,
                        d.ya_path,
                        d.mime_type,
                        d.document_url,
                        d.content_url,
                        d.full,
                        d.sharing_restricted,
                        m.lib_eval_method
                    FROM metadata m
                    JOIN document d ON d.md5 = m.md5
                    WHERE m.classification_id = :classification_id
                    ORDER BY d.md5 ASC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {
                    "classification_id": classification_id,
                    "limit": docs_page_size,
                    "offset": offset,
                },
            ).mappings().all()

            language_rows = conn.execute(
                text(
                    """
                    SELECT
                        COALESCE(d.language, 'unknown') AS language,
                        COUNT(*) AS count
                    FROM metadata m
                    JOIN document d ON d.md5 = m.md5
                    WHERE m.classification_id = :classification_id
                    GROUP BY COALESCE(d.language, 'unknown')
                    ORDER BY COUNT(*) DESC, COALESCE(d.language, 'unknown') ASC
                    """
                ),
                {"classification_id": classification_id},
            ).mappings().all()
        engine.dispose()

        total_pages = max(1, (docs_total + docs_page_size - 1) // docs_page_size)
        return {
            "available": True,
            "error": None,
            "config_source": config_source,
            "classification": {
                "classification_id": int(row.get("id") or 0),
                "ddc": str(row.get("ddc") or ""),
                "path": _format_path(row.get("path_en")),
                "path_tt": _format_path(row.get("path_tt")),
                "status": str(row.get("status") or ""),
                "created_by": str(row.get("created_by") or ""),
                "created_at": _serialize_value(row.get("created_at")),
                "usage_count": int(row.get("usage_count") or 0),
            },
            "linked_docs": {
                "page": docs_page,
                "page_size": docs_page_size,
                "total": docs_total,
                "total_pages": total_pages,
                "items": [
                    {
                        "md5": str(doc.get("md5") or ""),
                        "language": str(doc.get("language") or ""),
                        "ya_path": str(doc.get("ya_path") or ""),
                        "mime_type": str(doc.get("mime_type") or ""),
                        "document_url": str(doc.get("document_url") or ""),
                        "content_url": str(doc.get("content_url") or ""),
                        "full": bool(doc.get("full")) if doc.get("full") is not None else None,
                        "sharing_restricted": bool(doc.get("sharing_restricted"))
                        if doc.get("sharing_restricted") is not None
                        else None,
                        "lib_eval_method": str(doc.get("lib_eval_method") or ""),
                    }
                    for doc in docs_rows
                ],
            },
            "language_distribution": [
                {
                    "language": str(row.get("language") or ""),
                    "count": int(row.get("count") or 0),
                }
                for row in language_rows
            ],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "config_source": None,
            "classification": None,
            "linked_docs": {
                "page": 1,
                "page_size": docs_page_size,
                "total": 0,
                "total_pages": 1,
                "items": [],
            },
            "language_distribution": [],
        }


__all__ = [
    "list_classifications",
    "get_classification_insights",
    "get_normalization_preview",
    "get_merge_candidates",
    "get_classification_detail",
]
