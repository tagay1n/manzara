"""Library collection detection/review/apply helpers."""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List
from urllib.parse import unquote, urlparse

from sqlalchemy import text

from app.db import Database
from app.modules.library.stats import create_runtime_engine

_DEFAULT_PAGE_SIZE = 25
_MAX_PAGE_SIZE = 100
_MIN_ITEMS_PER_COLLECTION = 2
_DEFAULT_DETECT_SCAN_LIMIT = 12000
_DEFAULT_APPLY_COLLECTION_LIMIT = 500
_COLLECTION_STATUSES = {"suggested", "approved", "rejected"}
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NON_WORD_RE = re.compile(r"[^0-9A-Za-zА-Яа-яЁёӘәҖҗҢңӨөҮүҺһІіҒғҚқҪҫ]+", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")
_ISSUE_TOKEN_RE = re.compile(
    r"(?i)(?:^|\s)(?:№|#|issue|vol(?:ume)?|том|выпуск|сан|number|num)\s*[\w.-]+(?:\s|$)"
)
_TAIL_NUMBER_RE = re.compile(r"[\s._-](?:\d{1,4}|[ivxlcdm]{1,8})(?:[\s._-]*\d{0,4})?$", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runtime_schema() -> str:
    raw = str(os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip() or "monocorpus"
    if not _SCHEMA_RE.match(raw):
        return "monocorpus"
    return raw


def _set_search_path(conn: Any) -> None:
    schema = _runtime_schema()
    conn.execute(text(f'SET search_path TO "{schema}", public'))


def _safe_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return deepcopy(default)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return deepcopy(default)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return deepcopy(default)
        return parsed
    return deepcopy(default)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) != 0
    if value is None:
        return False
    text_value = str(value).strip().lower()
    return text_value in {"1", "true", "yes", "y", "on"}


def _normalize_text(value: Any) -> str:
    raw = str(value or "").strip().lower()
    raw = _NON_WORD_RE.sub(" ", raw)
    raw = _SPACE_RE.sub(" ", raw).strip()
    return raw


def _path_hint(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme and parsed.path:
        return unquote(parsed.path)
    return raw


def _path_parent(path_value: Any) -> str:
    hint = _path_hint(path_value)
    if not hint:
        return ""
    parent = str(PurePosixPath(hint).parent)
    if parent in {".", "/"}:
        return ""
    return parent


def _filename_stem(path_value: Any) -> str:
    hint = _path_hint(path_value)
    if not hint:
        return ""
    name = PurePosixPath(hint).name
    if "." in name:
        return name.rsplit(".", 1)[0]
    return name


def _extract_title(schema_obj: Dict[str, Any], fallback_path: Any) -> str:
    for key in ("name", "headline", "title", "alternateName"):
        value = schema_obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _filename_stem(fallback_path)


def _strip_issue_tokens(title: str) -> str:
    value = str(title or "").strip()
    if not value:
        return ""
    value = _ISSUE_TOKEN_RE.sub(" ", value)
    value = _YEAR_RE.sub(" ", value)
    value = _TAIL_NUMBER_RE.sub("", value)
    value = _SPACE_RE.sub(" ", value).strip(" -_/")
    return value


def _title_stem(title: str, path_value: Any) -> str:
    base = _strip_issue_tokens(title)
    if not base:
        base = _strip_issue_tokens(_filename_stem(path_value))
    normalized = _normalize_text(base)
    if normalized:
        return normalized
    return _normalize_text(_filename_stem(path_value))


def _has_issue_marker(title: str, path_value: Any) -> bool:
    combined = f"{title} {_filename_stem(path_value)}"
    if _ISSUE_TOKEN_RE.search(combined):
        return True
    if _YEAR_RE.search(combined):
        return True
    return bool(_TAIL_NUMBER_RE.search(_filename_stem(path_value)))


def _to_collection_item(row: Dict[str, Any]) -> Dict[str, Any]:
    schema_obj = _safe_json(row.get("schema_org"), {})
    if not isinstance(schema_obj, dict):
        schema_obj = {}
    path_value = row.get("ya_path") or row.get("document_url") or ""
    title = _extract_title(schema_obj, path_value)
    stripped = _strip_issue_tokens(title)
    stem = _title_stem(title, path_value)
    parent = _path_parent(path_value)
    return {
        "md5": str(row.get("md5") or ""),
        "title": title,
        "stripped_title": stripped,
        "stem": stem,
        "parent": parent,
        "path": str(path_value),
        "schema_org": schema_obj,
        "lib": _as_bool(row.get("lib")),
        "has_issue_marker": _has_issue_marker(title, path_value),
    }


def _candidate_groups(rows: Iterable[Dict[str, Any]], min_items: int) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        item = _to_collection_item(dict(row))
        if not item["md5"] or not item["stem"]:
            continue
        parent = item["parent"] or "_root"
        key = f"{parent}|{item['stem']}"
        grouped[key].append(item)

    candidates: List[Dict[str, Any]] = []
    for source_key, items in grouped.items():
        unique_md5 = {item["md5"] for item in items if item["md5"]}
        if len(unique_md5) < max(2, int(min_items)):
            continue

        titles = [item["stripped_title"] or item["title"] for item in items if item["title"]]
        title_counts = Counter([title for title in titles if title.strip()])
        preferred_title = ""
        if title_counts:
            preferred_title = sorted(
                title_counts.items(),
                key=lambda kv: (-int(kv[1]), -len(str(kv[0])), str(kv[0]).lower()),
            )[0][0]
        if not preferred_title:
            preferred_title = (items[0]["stripped_title"] or items[0]["title"] or items[0]["stem"]).strip()

        marker_count = sum(1 for item in items if item["has_issue_marker"])
        marker_ratio = marker_count / float(len(items)) if items else 0.0
        confidence = 0.55
        if marker_ratio >= 0.5:
            confidence += 0.20
        if source_key.split("|", 1)[0] not in {"", "_root"}:
            confidence += 0.10
        confidence += min(0.14, max(0.0, (len(items) - 2) * 0.03))
        confidence = round(min(confidence, 0.99), 4)

        representative = next(
            (item for item in items if item["lib"] and isinstance(item["schema_org"], dict)),
            items[0],
        )
        metadata_template = deepcopy(representative.get("schema_org") or {})
        if not isinstance(metadata_template, dict):
            metadata_template = {}
        if preferred_title and not str(metadata_template.get("name") or "").strip():
            metadata_template["name"] = preferred_title

        candidates.append(
            {
                "source_key": source_key,
                "title": preferred_title,
                "normalized_title": _normalize_text(preferred_title) or items[0]["stem"],
                "confidence": confidence,
                "item_count": len(items),
                "items": sorted(items, key=lambda item: (item["path"], item["md5"])),
                "heuristics": {
                    "key_mode": "parent+stem",
                    "parent": source_key.split("|", 1)[0],
                    "stem": source_key.split("|", 1)[1],
                    "marker_ratio": round(marker_ratio, 4),
                    "sample_titles": [str(title) for title in titles[:5]],
                },
                "metadata_template": metadata_template,
            }
        )
    candidates.sort(
        key=lambda item: (-int(item["item_count"]), -float(item["confidence"]), str(item["title"]).lower())
    )
    return candidates


def _collection_sort_sql(sort: str) -> str:
    mapping = {
        "updated_desc": "updated_at DESC, collection_id DESC",
        "updated_asc": "updated_at ASC, collection_id ASC",
        "items_desc": "item_count DESC, confidence DESC, title ASC",
        "items_asc": "item_count ASC, confidence DESC, title ASC",
        "confidence_desc": "confidence DESC, item_count DESC, title ASC",
        "confidence_asc": "confidence ASC, item_count DESC, title ASC",
        "title_asc": "title ASC, collection_id ASC",
        "title_desc": "title DESC, collection_id DESC",
    }
    return mapping.get(str(sort or "").strip().lower(), mapping["updated_desc"])


def _serialize_collection_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "collection_id": int(row.get("collection_id") or 0),
        "source_key": str(row.get("source_key") or ""),
        "title": str(row.get("title") or ""),
        "normalized_title": str(row.get("normalized_title") or ""),
        "status": str(row.get("status") or "suggested"),
        "include_in_library": _as_bool(row.get("include_in_library")),
        "confidence": float(row.get("confidence") or 0.0),
        "item_count": int(row.get("item_count") or 0),
        "heuristics": _safe_json(row.get("heuristics_json"), {}),
        "metadata_template": _safe_json(row.get("metadata_template_json"), {}),
        "notes": str(row.get("notes") or ""),
        "last_detected_at": row.get("last_detected_at"),
        "applied_at": row.get("applied_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _schema_names(value: Any) -> List[str]:
    values = value if isinstance(value, list) else [value]
    names: List[str] = []
    for item in values:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _schema_issue_number(schema_obj: Dict[str, Any]) -> str:
    about = schema_obj.get("about")
    terms = about if isinstance(about, list) else [about]
    for term in terms:
        if not isinstance(term, dict):
            continue
        term_set = _normalize_text(term.get("inDefinedTermSet"))
        if term_set not in {"issuenumber", "issue number"}:
            continue
        value = str(term.get("termCode") or term.get("name") or "").strip()
        if value:
            return value
    return ""


def _review_item(row: Dict[str, Any]) -> Dict[str, Any]:
    schema_obj = _safe_json(row.get("schema_org"), {})
    if not isinstance(schema_obj, dict):
        schema_obj = {}
    signal = _safe_json(row.get("signal_json"), {})
    if not isinstance(signal, dict):
        signal = {}
    path = str(row.get("item_hint") or "").strip()
    title = str(
        row.get("item_title")
        or schema_obj.get("name")
        or schema_obj.get("headline")
        or _filename_stem(path)
        or ""
    ).strip()
    work_type = str(schema_obj.get("@type") or "").strip()
    genres = _schema_names(schema_obj.get("genre"))
    publishers = _schema_names(schema_obj.get("publisher"))
    published = str(schema_obj.get("datePublished") or "").strip()
    pages_value = schema_obj.get("numberOfPages")
    try:
        pages = int(pages_value) if pages_value is not None else None
    except (TypeError, ValueError):
        pages = None
    parent = str(signal.get("parent") or _path_parent(path) or "").strip()
    return {
        "md5": str(row.get("md5") or ""),
        "title": title,
        "file_name": PurePosixPath(_path_hint(path)).name if path else "",
        "path": path,
        "included": _as_bool(row.get("lib")),
        "publication_date": published,
        "issue_number": _schema_issue_number(schema_obj),
        "publisher": publishers[0] if publishers else "",
        "publishers": publishers,
        "genre": genres[0] if genres else "",
        "genres": genres,
        "work_type": work_type,
        "number_of_pages": pages,
        "parent": parent,
        "has_issue_marker": _as_bool(signal.get("has_issue_marker")),
        "reasons": [],
    }


def _dominant_value(items: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    values: Dict[str, Dict[str, Any]] = {}
    for item in items:
        display = str(item.get(key) or "").strip()
        normalized = _normalize_text(display)
        if not normalized:
            continue
        entry = values.setdefault(normalized, {"display": display, "count": 0})
        entry["count"] += 1
    if not values:
        return {"dominant": None, "count": 0, "distinct": 0, "percent": 0.0}
    dominant = sorted(
        values.values(),
        key=lambda entry: (-int(entry["count"]), str(entry["display"]).lower()),
    )[0]
    total = len(items)
    count = int(dominant["count"])
    return {
        "dominant": str(dominant["display"]),
        "count": count,
        "distinct": len(values),
        "percent": round((count / total) * 100, 1) if total else 0.0,
    }


def _coverage(items: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    count = sum(
        1
        for item in items
        if item.get(key) is not None and item.get(key) != "" and item.get(key) != []
    )
    total = len(items)
    return {
        "count": count,
        "total": total,
        "percent": round((count / total) * 100, 1) if total else 0.0,
    }


def _sample_items(items: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    if len(items) <= limit:
        return [dict(item) for item in items]
    ordered = sorted(
        items,
        key=lambda item: (
            str(item.get("publication_date") or "9999"),
            str(item.get("file_name") or ""),
            str(item.get("md5") or ""),
        ),
    )
    indexes = {
        round(index * (len(ordered) - 1) / (limit - 1))
        for index in range(limit)
    }
    return [dict(ordered[index]) for index in sorted(indexes)]


def _build_collection_review_payload(
    collection: Dict[str, Any],
    rows: Iterable[Dict[str, Any]],
    *,
    sample_limit: int = 8,
    outlier_limit: int = 20,
) -> Dict[str, Any]:
    """Build bounded review evidence from all linked collection items."""
    sample_limit = max(3, min(int(sample_limit), 20))
    outlier_limit = max(1, min(int(outlier_limit), 100))
    items = [_review_item(dict(row)) for row in rows]
    consistency = {
        "title": _dominant_value(items, "title"),
        "publisher": _dominant_value(items, "publisher"),
        "genre": _dominant_value(items, "genre"),
        "work_type": _dominant_value(items, "work_type"),
        "parent": _dominant_value(items, "parent"),
    }
    heuristics = _safe_json(collection.get("heuristics_json"), {})
    if not isinstance(heuristics, dict):
        heuristics = {}
    marker_ratio = float(heuristics.get("marker_ratio") or 0.0)
    date_coverage = _coverage(items, "publication_date")
    issue_coverage = _coverage(items, "issue_number")
    mismatch_fields = {
        "title": "title_mismatch",
        "publisher": "publisher_mismatch",
        "genre": "genre_mismatch",
        "work_type": "type_mismatch",
        "parent": "parent_mismatch",
    }
    for item in items:
        reasons: List[str] = []
        for key, reason in mismatch_fields.items():
            dominant = consistency[key].get("dominant")
            current = item.get(key)
            pattern_is_stable = key in {"title", "parent"} or float(
                consistency[key].get("percent") or 0.0
            ) >= 90.0
            if (
                pattern_is_stable
                and dominant
                and current
                and _normalize_text(current) != _normalize_text(dominant)
            ):
                reasons.append(reason)
        if float(date_coverage["percent"]) >= 90.0 and not item.get("publication_date"):
            reasons.append("missing_date")
        if (
            marker_ratio >= 0.5
            and float(issue_coverage["percent"]) >= 90.0
            and not item.get("issue_number")
        ):
            reasons.append("missing_issue_number")
        item["reasons"] = reasons

    outliers = [dict(item) for item in items if item["reasons"]]
    outliers.sort(
        key=lambda item: (
            -len(item["reasons"]),
            str(item.get("publication_date") or "9999"),
            str(item.get("md5") or ""),
        )
    )
    dates = sorted(
        str(item["publication_date"])
        for item in items
        if str(item.get("publication_date") or "").strip()
    )
    summary = {
        "item_count": len(items),
        "included_count": sum(1 for item in items if item["included"]),
        "excluded_count": sum(1 for item in items if not item["included"]),
        "date_coverage": date_coverage,
        "issue_number_coverage": issue_coverage,
        "date_range": {
            "earliest": dates[0] if dates else None,
            "latest": dates[-1] if dates else None,
        },
    }
    evidence = [
        {
            "key": "shared_parent",
            "label": "Shared source folder",
            "value": str(heuristics.get("parent") or consistency["parent"].get("dominant") or "-"),
        },
        {
            "key": "normalized_stem",
            "label": "Normalized title stem",
            "value": str(heuristics.get("stem") or "-"),
        },
        {
            "key": "issue_markers",
            "label": "Issue marker detection",
            "value": f"{round(marker_ratio * 100)}%",
        },
    ]
    return {
        "collection_id": int(collection.get("collection_id") or 0),
        "collection": _serialize_collection_row(collection),
        "safety": {
            "approval_mutates_documents": False,
            "approval_effect": "Records the review decision only",
            "apply_task_id": "library.collection_apply",
        },
        "summary": summary,
        "grouping_evidence": evidence,
        "consistency": consistency,
        "outliers_total": len(outliers),
        "outliers": outliers[:outlier_limit],
        "samples": _sample_items(items, sample_limit),
    }


def detect_collections(
    *,
    scan_limit: int = _DEFAULT_DETECT_SCAN_LIMIT,
    min_items: int = _MIN_ITEMS_PER_COLLECTION,
) -> Dict[str, Any]:
    """Detect collection candidates and persist them for manual review."""
    scan_limit = max(200, min(int(scan_limit), 50000))
    min_items = max(2, min(int(min_items), 20))
    now = _utc_now()

    try:
        engine, config_source = create_runtime_engine()
        with engine.begin() as conn:
            _set_search_path(conn)
            rows = conn.execute(
                text(
                    """
                    SELECT
                        d.md5,
                        d.ya_path,
                        d.document_url,
                        m.lib,
                        m.schema_org
                    FROM metadata m
                    JOIN document d ON d.md5 = m.md5
                    WHERE m.schema_org IS NOT NULL
                    ORDER BY d.md5 ASC
                    LIMIT :limit
                    """
                ),
                {"limit": scan_limit},
            ).mappings().all()

            candidates = _candidate_groups(rows, min_items)
            persisted = 0
            linked_items = 0

            for candidate in candidates:
                row = conn.execute(
                    text(
                        """
                        INSERT INTO library_collections (
                            source_key,
                            title,
                            normalized_title,
                            status,
                            include_in_library,
                            confidence,
                            item_count,
                            heuristics_json,
                            metadata_template_json,
                            notes,
                            last_detected_at,
                            applied_at,
                            created_at,
                            updated_at
                        ) VALUES (
                            :source_key,
                            :title,
                            :normalized_title,
                            'suggested',
                            1,
                            :confidence,
                            :item_count,
                            :heuristics_json,
                            :metadata_template_json,
                            '',
                            :last_detected_at,
                            NULL,
                            :created_at,
                            :updated_at
                        )
                        ON CONFLICT(source_key) DO UPDATE SET
                            title = EXCLUDED.title,
                            normalized_title = EXCLUDED.normalized_title,
                            confidence = EXCLUDED.confidence,
                            item_count = EXCLUDED.item_count,
                            heuristics_json = EXCLUDED.heuristics_json,
                            metadata_template_json = CASE
                                WHEN library_collections.status = 'approved'
                                    THEN library_collections.metadata_template_json
                                ELSE EXCLUDED.metadata_template_json
                            END,
                            last_detected_at = EXCLUDED.last_detected_at,
                            updated_at = EXCLUDED.updated_at
                        RETURNING collection_id
                        """
                    ),
                    {
                        "source_key": candidate["source_key"],
                        "title": candidate["title"],
                        "normalized_title": candidate["normalized_title"],
                        "confidence": candidate["confidence"],
                        "item_count": candidate["item_count"],
                        "heuristics_json": json.dumps(
                            candidate["heuristics"], ensure_ascii=False, separators=(",", ":")
                        ),
                        "metadata_template_json": json.dumps(
                            candidate["metadata_template"], ensure_ascii=False, separators=(",", ":")
                        ),
                        "last_detected_at": now,
                        "created_at": now,
                        "updated_at": now,
                    },
                ).mappings().first()
                if not row:
                    continue
                collection_id = int(row.get("collection_id") or 0)
                if collection_id <= 0:
                    continue
                persisted += 1

                existing_rows = conn.execute(
                    text(
                        """
                        SELECT md5
                        FROM library_collection_items
                        WHERE collection_id = :collection_id
                        """
                    ),
                    {"collection_id": collection_id},
                ).mappings().all()
                existing_md5 = {str(item.get("md5") or "") for item in existing_rows}
                next_md5 = {str(item.get("md5") or "") for item in candidate["items"]}
                removed_md5 = [value for value in existing_md5 if value and value not in next_md5]

                for item in candidate["items"]:
                    md5 = str(item.get("md5") or "")
                    if not md5:
                        continue
                    linked_items += 1
                    conn.execute(
                        text(
                            """
                            INSERT INTO library_collection_items (
                                collection_id,
                                md5,
                                item_title,
                                item_hint,
                                signal_json,
                                created_at,
                                updated_at
                            ) VALUES (
                                :collection_id,
                                :md5,
                                :item_title,
                                :item_hint,
                                :signal_json,
                                :created_at,
                                :updated_at
                            )
                            ON CONFLICT(md5) DO UPDATE SET
                                collection_id = EXCLUDED.collection_id,
                                item_title = EXCLUDED.item_title,
                                item_hint = EXCLUDED.item_hint,
                                signal_json = EXCLUDED.signal_json,
                                updated_at = EXCLUDED.updated_at
                            """
                        ),
                        {
                            "collection_id": collection_id,
                            "md5": md5,
                            "item_title": item.get("title") or "",
                            "item_hint": item.get("path") or "",
                            "signal_json": json.dumps(
                                {
                                    "stem": item.get("stem"),
                                    "parent": item.get("parent"),
                                    "has_issue_marker": bool(item.get("has_issue_marker")),
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            "created_at": now,
                            "updated_at": now,
                        },
                    )

                for md5 in removed_md5:
                    conn.execute(
                        text(
                            """
                            DELETE FROM library_collection_items
                            WHERE collection_id = :collection_id
                              AND md5 = :md5
                            """
                        ),
                        {"collection_id": collection_id, "md5": md5},
                    )

            conn.execute(
                text(
                    """
                    INSERT INTO library_collection_events (action, payload_json, created_at)
                    VALUES (:action, :payload_json, :created_at)
                    """
                ),
                {
                    "action": "detect",
                    "payload_json": json.dumps(
                        {
                            "scan_limit": scan_limit,
                            "min_items": min_items,
                            "persisted": persisted,
                            "linked_items": linked_items,
                        },
                        ensure_ascii=False,
                    ),
                    "created_at": now,
                },
            )

        engine.dispose()
        return {
            "available": True,
            "error": None,
            "config_source": config_source,
            "scan_limit": scan_limit,
            "min_items": min_items,
            "collections_detected": len(candidates),
            "collections_persisted": persisted,
            "items_linked": linked_items,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "config_source": None,
            "scan_limit": scan_limit,
            "min_items": min_items,
            "collections_detected": 0,
            "collections_persisted": 0,
            "items_linked": 0,
        }


def _derive_template_from_items(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    for row in items:
        schema_obj = _safe_json(row.get("schema_org"), {})
        if isinstance(schema_obj, dict) and schema_obj:
            return deepcopy(schema_obj)
    return {}


def _compose_collection_schema(
    template: Dict[str, Any],
    current_schema: Dict[str, Any],
    *,
    collection_title: str,
    item_title: str,
    md5: str,
) -> Dict[str, Any]:
    payload = deepcopy(template) if isinstance(template, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    if not payload.get("@context"):
        payload["@context"] = "https://schema.org"
    if not payload.get("@type"):
        payload["@type"] = "CreativeWork"
    if collection_title and not str(payload.get("name") or "").strip():
        payload["name"] = collection_title
    if item_title:
        payload["position"] = item_title
    payload["isPartOf"] = {
        "@type": "Collection",
        "name": collection_title or str(payload.get("name") or ""),
    }

    if isinstance(current_schema, dict):
        for key in ("identifier", "url", "sameAs", "inLanguage", "datePublished"):
            if key in current_schema and key not in payload:
                payload[key] = deepcopy(current_schema[key])
    if md5 and "identifier" not in payload:
        payload["identifier"] = md5
    return payload


def apply_collection_overrides(
    *,
    collection_limit: int = _DEFAULT_APPLY_COLLECTION_LIMIT,
) -> Dict[str, Any]:
    """Apply approved collection-level metadata and include overrides."""
    collection_limit = max(1, min(int(collection_limit), 5000))
    now = _utc_now()
    try:
        engine, config_source = create_runtime_engine()
        with engine.begin() as conn:
            _set_search_path(conn)
            collections = conn.execute(
                text(
                    """
                    SELECT
                        collection_id,
                        title,
                        metadata_template_json
                    FROM library_collections
                    WHERE status = 'approved'
                      AND include_in_library = 1
                    ORDER BY updated_at ASC, collection_id ASC
                    LIMIT :limit
                    """
                ),
                {"limit": collection_limit},
            ).mappings().all()

            collections_applied = 0
            items_applied = 0
            forced_include_count = 0

            for collection in collections:
                collection_id = int(collection.get("collection_id") or 0)
                if collection_id <= 0:
                    continue
                items = conn.execute(
                    text(
                        """
                        SELECT
                            i.md5,
                            i.item_title,
                            m.lib,
                            m.schema_org
                        FROM library_collection_items i
                        JOIN metadata m ON m.md5 = i.md5
                        WHERE i.collection_id = :collection_id
                        ORDER BY i.md5 ASC
                        """
                    ),
                    {"collection_id": collection_id},
                ).mappings().all()
                if not items:
                    continue

                template = _safe_json(collection.get("metadata_template_json"), {})
                if not isinstance(template, dict) or not template:
                    template = _derive_template_from_items([dict(item) for item in items])
                if not isinstance(template, dict):
                    template = {}
                title = str(collection.get("title") or "").strip()
                if title and not str(template.get("name") or "").strip():
                    template["name"] = title

                for item in items:
                    md5 = str(item.get("md5") or "")
                    if not md5:
                        continue
                    current_schema = _safe_json(item.get("schema_org"), {})
                    if not isinstance(current_schema, dict):
                        current_schema = {}
                    schema_payload = _compose_collection_schema(
                        template,
                        current_schema,
                        collection_title=title,
                        item_title=str(item.get("item_title") or ""),
                        md5=md5,
                    )
                    was_included = _as_bool(item.get("lib"))
                    if not was_included:
                        forced_include_count += 1
                    conn.execute(
                        text(
                            """
                            UPDATE metadata
                            SET lib = TRUE,
                                lib_eval_method = 'collection_override',
                                schema_org = CAST(:schema_json AS JSON)
                            WHERE md5 = :md5
                            """
                        ),
                        {
                            "schema_json": json.dumps(schema_payload, ensure_ascii=False),
                            "md5": md5,
                        },
                    )
                    items_applied += 1

                conn.execute(
                    text(
                        """
                        UPDATE library_collections
                        SET applied_at = :applied_at,
                            updated_at = :updated_at
                        WHERE collection_id = :collection_id
                        """
                    ),
                    {
                        "collection_id": collection_id,
                        "applied_at": now,
                        "updated_at": now,
                    },
                )
                collections_applied += 1

            conn.execute(
                text(
                    """
                    INSERT INTO library_collection_events (action, payload_json, created_at)
                    VALUES (:action, :payload_json, :created_at)
                    """
                ),
                {
                    "action": "apply",
                    "payload_json": json.dumps(
                        {
                            "collections_applied": collections_applied,
                            "items_applied": items_applied,
                            "forced_include_count": forced_include_count,
                        },
                        ensure_ascii=False,
                    ),
                    "created_at": now,
                },
            )

        engine.dispose()
        return {
            "available": True,
            "error": None,
            "config_source": config_source,
            "collections_applied": collections_applied,
            "items_applied": items_applied,
            "forced_include_count": forced_include_count,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "config_source": None,
            "collections_applied": 0,
            "items_applied": 0,
            "forced_include_count": 0,
        }


def get_collection_overview(top_limit: int = 12) -> Dict[str, Any]:
    """Return summary stats for collection review page."""
    top_limit = max(1, min(int(top_limit), 80))
    try:
        engine, config_source = create_runtime_engine()
        with engine.connect() as conn:
            _set_search_path(conn)
            stats_row = conn.execute(
                text(
                    """
                    SELECT
                        COUNT(*) AS total_collections,
                        SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved_collections,
                        SUM(CASE WHEN status = 'suggested' THEN 1 ELSE 0 END) AS suggested_collections,
                        SUM(CASE WHEN include_in_library = 1 THEN 1 ELSE 0 END) AS included_collections,
                        COALESCE(SUM(item_count), 0) AS items_linked
                    FROM library_collections
                    """
                )
            ).mappings().first() or {}
            top_rows = conn.execute(
                text(
                    """
                    SELECT *
                    FROM library_collections
                    ORDER BY item_count DESC, confidence DESC, updated_at DESC
                    LIMIT :limit
                    """
                ),
                {"limit": top_limit},
            ).mappings().all()
        engine.dispose()

        return {
            "available": True,
            "error": None,
            "config_source": config_source,
            "stats": {
                "total_collections": int(stats_row.get("total_collections") or 0),
                "approved_collections": int(stats_row.get("approved_collections") or 0),
                "suggested_collections": int(stats_row.get("suggested_collections") or 0),
                "included_collections": int(stats_row.get("included_collections") or 0),
                "items_linked": int(stats_row.get("items_linked") or 0),
            },
            "top_collections": [_serialize_collection_row(dict(row)) for row in top_rows],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "config_source": None,
            "stats": {
                "total_collections": 0,
                "approved_collections": 0,
                "suggested_collections": 0,
                "included_collections": 0,
                "items_linked": 0,
            },
            "top_collections": [],
        }


def list_collections(
    *,
    search: str = "",
    status: str = "",
    include: str = "all",
    page: int = 1,
    page_size: int = _DEFAULT_PAGE_SIZE,
    sort: str = "updated_desc",
) -> Dict[str, Any]:
    """Return paginated collection rows."""
    page = max(1, int(page))
    page_size = max(1, min(_MAX_PAGE_SIZE, int(page_size)))
    include = str(include or "all").strip().lower()
    if include not in {"all", "yes", "no"}:
        include = "all"
    status = str(status or "").strip().lower()
    if status and status not in _COLLECTION_STATUSES:
        status = ""
    search_value = str(search or "").strip().lower()
    offset = (page - 1) * page_size

    params = {
        "search": search_value,
        "search_like": f"%{search_value}%",
        "status": status,
        "include": include,
        "limit": page_size,
        "offset": offset,
    }
    sort_sql = _collection_sort_sql(sort)

    try:
        engine, config_source = create_runtime_engine()
        with engine.connect() as conn:
            _set_search_path(conn)
            total = int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*) AS count
                        FROM library_collections
                        WHERE (:search = '' OR LOWER(title) LIKE :search_like OR source_key LIKE :search_like)
                          AND (:status = '' OR status = :status)
                          AND (
                              :include = 'all'
                              OR (:include = 'yes' AND include_in_library = 1)
                              OR (:include = 'no' AND include_in_library = 0)
                          )
                        """
                    ),
                    params,
                ).scalar()
                or 0
            )
            rows = conn.execute(
                text(
                    f"""
                    SELECT *
                    FROM library_collections
                    WHERE (:search = '' OR LOWER(title) LIKE :search_like OR source_key LIKE :search_like)
                      AND (:status = '' OR status = :status)
                      AND (
                          :include = 'all'
                          OR (:include = 'yes' AND include_in_library = 1)
                          OR (:include = 'no' AND include_in_library = 0)
                      )
                    ORDER BY {sort_sql}
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            ).mappings().all()
        engine.dispose()

        return {
            "available": True,
            "error": None,
            "config_source": config_source,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": max(1, (total + page_size - 1) // page_size),
            "items": [_serialize_collection_row(dict(row)) for row in rows],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "config_source": None,
            "page": page,
            "page_size": page_size,
            "total": 0,
            "total_pages": 1,
            "items": [],
        }


def get_collection_insights(
    *,
    cluster_limit: int = 24,
    queue_limit: int = 40,
) -> Dict[str, Any]:
    """Return compact insights for collection tabs."""
    cluster_limit = max(1, min(int(cluster_limit), 200))
    queue_limit = max(1, min(int(queue_limit), 200))
    try:
        engine, config_source = create_runtime_engine()
        with engine.connect() as conn:
            _set_search_path(conn)
            clusters_rows = conn.execute(
                text(
                    """
                    SELECT *
                    FROM library_collections
                    ORDER BY item_count DESC, confidence DESC, updated_at DESC
                    LIMIT :limit
                    """
                ),
                {"limit": cluster_limit},
            ).mappings().all()
            queue_total = int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*) AS count
                        FROM library_collections
                        WHERE status = 'suggested'
                        """
                    )
                ).scalar()
                or 0
            )
            queue_rows = conn.execute(
                text(
                    """
                    SELECT *
                    FROM library_collections
                    WHERE status = 'suggested'
                    ORDER BY confidence DESC, item_count DESC, updated_at DESC
                    LIMIT :limit
                    """
                ),
                {"limit": queue_limit},
            ).mappings().all()
        engine.dispose()

        clusters = [_serialize_collection_row(dict(row)) for row in clusters_rows]
        queue_items = [_serialize_collection_row(dict(row)) for row in queue_rows]
        return {
            "available": True,
            "error": None,
            "config_source": config_source,
            "summary": {
                "cluster_count": len(clusters),
                "queue_total": queue_total,
            },
            "clusters": clusters,
            "queue": {
                "total": queue_total,
                "items": queue_items,
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "config_source": None,
            "summary": {
                "cluster_count": 0,
                "queue_total": 0,
            },
            "clusters": [],
            "queue": {"total": 0, "items": []},
        }


def list_collection_items(
    collection_id: int,
    *,
    limit: int = 400,
) -> Dict[str, Any]:
    """Return items currently attached to one collection."""
    collection_id = int(collection_id)
    if collection_id <= 0:
        return {
            "available": False,
            "error": "collection_id must be positive",
            "collection_id": collection_id,
            "collection": None,
            "items": [],
        }
    limit = max(1, min(int(limit), 2000))
    try:
        engine, _config_source = create_runtime_engine()
        with engine.connect() as conn:
            _set_search_path(conn)
            collection_row = conn.execute(
                text(
                    """
                    SELECT *
                    FROM library_collections
                    WHERE collection_id = :collection_id
                    """
                ),
                {"collection_id": collection_id},
            ).mappings().first()
            if not collection_row:
                engine.dispose()
                return {
                    "available": False,
                    "error": "Collection not found",
                    "collection_id": collection_id,
                    "collection": None,
                    "items": [],
                }

            rows = conn.execute(
                text(
                    """
                    SELECT
                        i.collection_id,
                        i.md5,
                        i.item_title,
                        i.item_hint,
                        i.signal_json,
                        d.ya_path,
                        d.document_url,
                        m.lib,
                        m.schema_org
                    FROM library_collection_items i
                    LEFT JOIN document d ON d.md5 = i.md5
                    LEFT JOIN metadata m ON m.md5 = i.md5
                    WHERE i.collection_id = :collection_id
                    ORDER BY i.item_title ASC, i.md5 ASC
                    LIMIT :limit
                    """
                ),
                {"collection_id": collection_id, "limit": limit},
            ).mappings().all()
        engine.dispose()

        items = []
        for row in rows:
            schema_obj = _safe_json(row.get("schema_org"), {})
            schema_name = ""
            if isinstance(schema_obj, dict):
                schema_name = str(schema_obj.get("name") or schema_obj.get("headline") or "").strip()
            items.append(
                {
                    "collection_id": int(row.get("collection_id") or collection_id),
                    "md5": str(row.get("md5") or ""),
                    "item_title": str(row.get("item_title") or ""),
                    "item_hint": str(row.get("item_hint") or ""),
                    "ya_path": str(row.get("ya_path") or ""),
                    "document_url": str(row.get("document_url") or ""),
                    "lib": _as_bool(row.get("lib")),
                    "schema_name": schema_name,
                    "signal": _safe_json(row.get("signal_json"), {}),
                }
            )

        return {
            "available": True,
            "error": None,
            "collection_id": collection_id,
            "collection": _serialize_collection_row(dict(collection_row)),
            "items": items,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "collection_id": collection_id,
            "collection": None,
            "items": [],
        }


def get_collection_review(
    collection_id: int,
    *,
    sample_limit: int = 8,
    outlier_limit: int = 20,
) -> Dict[str, Any]:
    """Return bounded evidence for reviewing one collection candidate."""
    collection_id = int(collection_id)
    if collection_id <= 0:
        return {
            "available": False,
            "error": "collection_id must be positive",
            "config_source": None,
            "collection_id": collection_id,
        }
    try:
        engine, config_source = create_runtime_engine()
        with engine.connect() as conn:
            _set_search_path(conn)
            collection = conn.execute(
                text(
                    """
                    SELECT *
                    FROM library_collections
                    WHERE collection_id = :collection_id
                    """
                ),
                {"collection_id": collection_id},
            ).mappings().first()
            if not collection:
                engine.dispose()
                return {
                    "available": False,
                    "error": "Collection not found",
                    "config_source": config_source,
                    "collection_id": collection_id,
                }
            rows = conn.execute(
                text(
                    """
                    SELECT
                        i.md5,
                        i.item_title,
                        i.item_hint,
                        i.signal_json,
                        m.lib,
                        m.schema_org
                    FROM library_collection_items i
                    LEFT JOIN metadata m ON m.md5 = i.md5
                    WHERE i.collection_id = :collection_id
                    ORDER BY i.md5 ASC
                    """
                ),
                {"collection_id": collection_id},
            ).mappings().all()
        engine.dispose()
        return {
            "available": True,
            "error": None,
            "config_source": config_source,
            **_build_collection_review_payload(
                dict(collection),
                [dict(row) for row in rows],
                sample_limit=sample_limit,
                outlier_limit=outlier_limit,
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "config_source": None,
            "collection_id": collection_id,
        }


def update_collection(
    db: Database,
    collection_id: int,
    updates: Dict[str, Any],
) -> Dict[str, Any]:
    """Patch one collection review record and emit SSE/system events."""
    collection_id = int(collection_id)
    if collection_id <= 0:
        raise ValueError("collection_id must be positive")

    allowed = {"status", "include_in_library", "title", "notes"}
    payload: Dict[str, Any] = {}
    for key in allowed:
        if key not in updates:
            continue
        value = updates[key]
        if key == "status":
            normalized = str(value or "").strip().lower()
            if normalized not in _COLLECTION_STATUSES:
                raise ValueError("status must be one of: suggested, approved, rejected")
            payload[key] = normalized
        elif key == "include_in_library":
            payload[key] = 1 if _as_bool(value) else 0
        elif key in {"title", "notes"}:
            payload[key] = str(value or "").strip()

    if not payload:
        return {
            "ok": False,
            "error": "No supported fields in request",
            "collection": None,
            "updated_fields": [],
        }

    now = _utc_now()
    try:
        engine, _config_source = create_runtime_engine()
        with engine.begin() as conn:
            _set_search_path(conn)
            current_row = conn.execute(
                text(
                    """
                    SELECT *
                    FROM library_collections
                    WHERE collection_id = :collection_id
                    """
                ),
                {"collection_id": collection_id},
            ).mappings().first()
            if not current_row:
                engine.dispose()
                raise ValueError("Collection not found")

            assignments = []
            params: Dict[str, Any] = {"collection_id": collection_id, "updated_at": now}
            for key, value in payload.items():
                assignments.append(f"{key} = :{key}")
                params[key] = value
            assignments.append("updated_at = :updated_at")

            row = conn.execute(
                text(
                    f"""
                    UPDATE library_collections
                    SET {", ".join(assignments)}
                    WHERE collection_id = :collection_id
                    RETURNING *
                    """
                ),
                params,
            ).mappings().first()
            if not row:
                engine.dispose()
                raise ValueError("Collection not found")

            conn.execute(
                text(
                    """
                    INSERT INTO library_collection_events (action, payload_json, created_at)
                    VALUES (:action, :payload_json, :created_at)
                    """
                ),
                {
                    "action": "collection.updated",
                    "payload_json": json.dumps(
                        {
                            "collection_id": collection_id,
                            "updated_fields": sorted(payload.keys()),
                            "updates": payload,
                        },
                        ensure_ascii=False,
                    ),
                    "created_at": now,
                },
            )
        engine.dispose()

        db.insert_event(
            "library.collections.updated",
            task_id=None,
            run_id=None,
            panel_id="library",
            payload={
                "collection_id": collection_id,
                "updated_fields": sorted(payload.keys()),
            },
        )

        return {
            "ok": True,
            "error": None,
            "collection": _serialize_collection_row(dict(row)),
            "updated_fields": sorted(payload.keys()),
        }
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc),
            "collection": None,
            "updated_fields": sorted(payload.keys()),
        }


__all__ = [
    "apply_collection_overrides",
    "detect_collections",
    "get_collection_insights",
    "get_collection_overview",
    "get_collection_review",
    "list_collection_items",
    "list_collections",
    "update_collection",
]
