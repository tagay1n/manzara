"""Library classification normalization, merge, and detail operations."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable

from sqlalchemy import text

from app.modules.library.classification_insights import (
    _DEFAULT_ALL_ROWS_LIMIT,
    _DEFAULT_DROP_SEGMENTS,
    _all_classification_usage_rows,
    _digits_prefix,
    _format_path,
    _parse_json_path,
    _rewrite_schema_org_classification_terms,
    _serialize_value,
)
from app.modules.library.response_envelope import available_payload, unavailable_payload
from app.modules.library.stats import create_runtime_engine, dispose_runtime_engine


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

        return available_payload(
            config_source=config_source,
            rules={
                "drop_segments": sorted(drop_set),
            },
            summary={
                "total_rows_scanned": len(rows),
                "affected_classifications": len(affected),
                "estimated_reassigned_documents": sum(int(item["usage_count"]) for item in affected),
                "merge_group_candidates": len(merge_groups),
            },
            affected_preview=affected[: max(1, int(limit))],
            merge_groups=merge_groups[: max(1, min(int(limit), 80))],
        )
    except Exception as exc:  # noqa: BLE001
        return unavailable_payload(
            exc,
            rules={"drop_segments": []},
            summary={
                "total_rows_scanned": 0,
                "affected_classifications": 0,
                "estimated_reassigned_documents": 0,
                "merge_group_candidates": 0,
            },
            affected_preview=[],
            merge_groups=[],
        )


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
        return available_payload(
            config_source=config_source,
            summary={
                "rows_scanned": len(items),
                "candidate_count": len(candidates),
                "min_score": min_score,
            },
            candidates=candidates[: max(1, int(limit))],
        )
    except Exception as exc:  # noqa: BLE001
        return unavailable_payload(
            exc,
            summary={
                "rows_scanned": 0,
                "candidate_count": 0,
                "min_score": min_score,
            },
            candidates=[],
        )


def merge_classifications(
    *,
    source_classification_id: int,
    target_classification_id: int,
    reason: str = "",
) -> Dict[str, Any]:
    """Merge one classification into another and keep schema_org terms aligned."""
    source_id = int(source_classification_id)
    target_id = int(target_classification_id)
    if source_id <= 0 or target_id <= 0:
        raise ValueError("source_classification_id and target_classification_id must be positive")
    if source_id == target_id:
        raise ValueError("source_classification_id and target_classification_id must differ")

    engine, config_source = create_runtime_engine()
    try:
        with engine.begin() as conn:
            source = conn.execute(
                text(
                    """
                    SELECT id, ddc, path_en
                    FROM classification
                    WHERE id = :classification_id
                    FOR UPDATE
                    """
                ),
                {"classification_id": source_id},
            ).mappings().first()
            if not source:
                raise ValueError("Source classification not found")

            target = conn.execute(
                text(
                    """
                    SELECT id, ddc, path_en
                    FROM classification
                    WHERE id = :classification_id
                    FOR UPDATE
                    """
                ),
                {"classification_id": target_id},
            ).mappings().first()
            if not target:
                raise ValueError("Target classification not found")

            target_ddc = str(target.get("ddc") or "").strip()
            target_path_parts = _parse_json_path(target.get("path_en"))
            if not target_ddc or not target_path_parts:
                raise ValueError("Target classification is incomplete (missing ddc/path)")

            moved_rows = conn.execute(
                text(
                    """
                    SELECT md5, schema_org
                    FROM metadata
                    WHERE classification_id = :source_id
                    FOR UPDATE
                    """
                ),
                {"source_id": source_id},
            ).mappings().all()
            moved_docs_count = len(moved_rows)

            relinked = conn.execute(
                text(
                    """
                    UPDATE metadata
                    SET classification_id = :target_id
                    WHERE classification_id = :source_id
                    """
                ),
                {"source_id": source_id, "target_id": target_id},
            )
            relinked_count = int(relinked.rowcount or 0)

            schema_org_updated_count = 0
            for row in moved_rows:
                md5 = str(row.get("md5") or "").strip()
                if not md5:
                    continue
                updated_schema, changed = _rewrite_schema_org_classification_terms(
                    row.get("schema_org"),
                    target_ddc=target_ddc,
                    target_path_parts=target_path_parts,
                )
                if not changed:
                    continue
                conn.execute(
                    text(
                        """
                        UPDATE metadata
                        SET schema_org = CAST(:schema_json AS JSON)
                        WHERE md5 = :md5
                        """
                    ),
                    {
                        "md5": md5,
                        "schema_json": json.dumps(updated_schema, ensure_ascii=False),
                    },
                )
                schema_org_updated_count += 1

            deleted = conn.execute(
                text("DELETE FROM classification WHERE id = :source_id"),
                {"source_id": source_id},
            )
            source_deleted = int(deleted.rowcount or 0) > 0
            if not source_deleted:
                raise ValueError("Source classification could not be deleted")

        return available_payload(
            config_source=config_source,
            source_classification_id=source_id,
            target_classification_id=target_id,
            moved_docs_count=moved_docs_count,
            relinked_count=relinked_count,
            schema_org_updated_count=schema_org_updated_count,
            source_deleted=True,
            reason=str(reason or "").strip(),
        )
    finally:
        dispose_runtime_engine(engine)


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
        dispose_runtime_engine(engine)

        total_pages = max(1, (docs_total + docs_page_size - 1) // docs_page_size)
        return available_payload(
            config_source=config_source,
            classification={
                "classification_id": int(row.get("id") or 0),
                "ddc": str(row.get("ddc") or ""),
                "path": _format_path(row.get("path_en")),
                "path_tt": _format_path(row.get("path_tt")),
                "status": str(row.get("status") or ""),
                "created_by": str(row.get("created_by") or ""),
                "created_at": _serialize_value(row.get("created_at")),
                "usage_count": int(row.get("usage_count") or 0),
            },
            linked_docs={
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
            language_distribution=[
                {
                    "language": str(row.get("language") or ""),
                    "count": int(row.get("count") or 0),
                }
                for row in language_rows
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return unavailable_payload(
            exc,
            classification=None,
            linked_docs={
                "page": 1,
                "page_size": docs_page_size,
                "total": 0,
                "total_pages": 1,
                "items": [],
            },
            language_distribution=[],
        )



__all__ = [
    "get_normalization_preview",
    "get_merge_candidates",
    "merge_classifications",
    "get_classification_detail",
]
