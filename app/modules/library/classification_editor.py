"""Atomic staged editing operations for the Library classification taxonomy."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from sqlalchemy import text

from app.modules.library.classification_insights import (
    _parse_json_path,
    _rewrite_schema_org_classification_terms,
)
from app.modules.library.metadata_contract import is_english_facet
from app.modules.library.response_envelope import available_payload
from app.modules.library.stats import create_runtime_engine, dispose_runtime_engine


class StaleTaxonomyError(ValueError):
    """The client edited an older taxonomy snapshot."""


def _path_key(path: Iterable[str]) -> str:
    return "|".join(part.casefold() for part in path)


def _taxonomy_revision(rows: Iterable[Mapping[str, Any]]) -> str:
    payload = [
        {
            "id": int(row.get("id") or 0),
            "ddc": str(row.get("ddc") or ""),
            "path": _parse_json_path(row.get("path_en")),
        }
        for row in rows
    ]
    payload.sort(key=lambda item: item["id"])
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _valid_id(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _valid_path(value: Any) -> list[str]:
    if not isinstance(value, list) or not 2 <= len(value) <= 8:
        raise ValueError("classification path must contain 2 to 8 levels")
    path: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise ValueError("classification path levels must be strings")
        label = " ".join(raw.split())
        if not label or len(label) > 180 or not is_english_facet(label):
            raise ValueError(f"invalid English classification label: {raw!r}")
        path.append(label)
    return path


def _prepare_change_set(
    rows: list[Mapping[str, Any]], payload: Mapping[str, Any]
) -> dict[str, Any]:
    current_revision = _taxonomy_revision(rows)
    base_revision = str(payload.get("base_revision") or "")
    if base_revision != current_revision:
        raise StaleTaxonomyError("The taxonomy changed; refresh before applying this draft")

    by_id = {int(row.get("id") or 0): dict(row) for row in rows}
    desired_paths = {
        classification_id: _parse_json_path(row.get("path_en"))
        for classification_id, row in by_id.items()
    }
    changes: list[dict[str, Any]] = []
    seen_changes: set[int] = set()
    raw_changes = payload.get("changes") or []
    if not isinstance(raw_changes, list):
        raise ValueError("changes must be an array")
    for item in raw_changes:
        if not isinstance(item, Mapping):
            raise ValueError("each classification change must be an object")
        classification_id = _valid_id(item.get("classification_id"), "classification_id")
        if classification_id not in by_id:
            raise ValueError(f"classification #{classification_id} was not found")
        if classification_id in seen_changes:
            raise ValueError(f"classification #{classification_id} has duplicate changes")
        seen_changes.add(classification_id)
        path = _valid_path(item.get("path"))
        desired_paths[classification_id] = path
        changes.append({"classification_id": classification_id, "path": path})

    merges: list[dict[str, int]] = []
    merged_sources: set[int] = set()
    raw_merges = payload.get("merges") or []
    if not isinstance(raw_merges, list):
        raise ValueError("merges must be an array")
    for item in raw_merges:
        if not isinstance(item, Mapping):
            raise ValueError("each classification merge must be an object")
        source_id = _valid_id(item.get("source_classification_id"), "source_classification_id")
        target_id = _valid_id(item.get("target_classification_id"), "target_classification_id")
        if source_id == target_id:
            raise ValueError("a classification cannot be merged into itself")
        if source_id not in by_id or target_id not in by_id:
            raise ValueError("merge source and target classifications must exist")
        if source_id in merged_sources:
            raise ValueError(f"classification #{source_id} has duplicate merge targets")
        merged_sources.add(source_id)
        merges.append(
            {
                "source_classification_id": source_id,
                "target_classification_id": target_id,
            }
        )
    if any(item["target_classification_id"] in merged_sources for item in merges):
        raise ValueError("merge targets cannot also be merge sources")
    if merged_sources & seen_changes:
        raise ValueError("merge sources cannot also have path changes")

    changes = [
        item
        for item in changes
        if item["path"] != _parse_json_path(by_id[item["classification_id"]].get("path_en"))
    ]

    identities: dict[tuple[str, str], int] = {}
    for classification_id, row in by_id.items():
        if classification_id in merged_sources:
            continue
        key = (str(row.get("ddc") or "").strip(), _path_key(desired_paths[classification_id]))
        previous = identities.get(key)
        if previous is not None:
            raise ValueError(
                f"classifications #{previous} and #{classification_id} would have the same DDC and path"
            )
        identities[key] = classification_id

    normalized = {
        "base_revision": current_revision,
        "changes": sorted(changes, key=lambda item: item["classification_id"]),
        "merges": sorted(merges, key=lambda item: item["source_classification_id"]),
    }
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    change_set_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    moved_docs = sum(int(by_id[item["source_classification_id"]].get("usage_count") or 0) for item in merges)
    changed_ids = {
        item["classification_id"]
        for item in changes
        if item["path"] != _parse_json_path(by_id[item["classification_id"]].get("path_en"))
    }
    affected_ids = changed_ids | merged_sources
    affected_docs = sum(int(by_id[item].get("usage_count") or 0) for item in affected_ids)
    return {
        **normalized,
        "change_set_hash": change_set_hash,
        "summary": {
            "path_changes": len(changed_ids),
            "classification_merges": len(merges),
            "moved_documents": moved_docs,
            "affected_documents": affected_docs,
            "schema_org_updates": affected_docs,
        },
    }


def _all_rows(conn: Any, *, lock: bool = False) -> list[dict[str, Any]]:
    suffix = " FOR UPDATE OF c" if lock else ""
    return [
        dict(row)
        for row in conn.execute(
            text(
                """
                SELECT c.id,c.ddc,c.path_en,COALESCE(u.usage_count,0) usage_count
                FROM classification c
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) usage_count FROM metadata m
                    WHERE m.classification_id=c.id
                ) u ON TRUE
                ORDER BY c.id
                """
                + suffix
            )
        ).mappings()
    ]


def preview_classification_change_set(payload: Mapping[str, Any]) -> dict[str, Any]:
    engine, source = create_runtime_engine()
    try:
        with engine.connect() as conn:
            plan = _prepare_change_set(_all_rows(conn), payload)
        return available_payload(config_source=source, **plan)
    finally:
        dispose_runtime_engine(engine)


def apply_classification_change_set(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("confirmed") is not True:
        raise ValueError("confirmed must be true")
    requested_hash = str(payload.get("change_set_hash") or "")
    engine, source = create_runtime_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text("LOCK TABLE classification IN SHARE ROW EXCLUSIVE MODE"))
            conn.execute(text("LOCK TABLE metadata IN SHARE ROW EXCLUSIVE MODE"))
            rows = _all_rows(conn, lock=True)
            plan = _prepare_change_set(rows, payload)
            if not requested_hash or requested_hash != plan["change_set_hash"]:
                raise ValueError("change_set_hash does not match the reviewed change set")

            by_id = {int(row["id"]): row for row in rows}
            desired_paths = {
                int(row["id"]): _parse_json_path(row.get("path_en")) for row in rows
            }
            desired_paths.update(
                {item["classification_id"]: item["path"] for item in plan["changes"]}
            )
            merge_targets = {
                item["source_classification_id"]: item["target_classification_id"]
                for item in plan["merges"]
            }
            affected_ids = set(merge_targets) | {
                item["classification_id"] for item in plan["changes"]
            }
            docs = [
                dict(row)
                for row in conn.execute(
                    text(
                        "SELECT md5,classification_id,schema_org FROM metadata "
                        "WHERE classification_id=ANY(:ids) FOR UPDATE"
                    ),
                    {"ids": list(affected_ids)},
                ).mappings()
            ] if affected_ids else []

            for source_id, target_id in merge_targets.items():
                conn.execute(
                    text("UPDATE metadata SET classification_id=:target WHERE classification_id=:source"),
                    {"source": source_id, "target": target_id},
                )
            if merge_targets:
                conn.execute(
                    text("DELETE FROM classification WHERE id=ANY(:ids)"),
                    {"ids": list(merge_targets)},
                )

            survivors = [
                item for item in plan["changes"] if item["classification_id"] not in merge_targets
            ]
            for item in survivors:
                conn.execute(
                    text("UPDATE classification SET path_en_key=:key WHERE id=:id"),
                    {"id": item["classification_id"], "key": f"__taxonomy_edit__{item['classification_id']}"},
                )
            for item in survivors:
                conn.execute(
                    text(
                        "UPDATE classification SET path_en=CAST(:path AS JSON),path_en_key=:key WHERE id=:id"
                    ),
                    {
                        "id": item["classification_id"],
                        "path": json.dumps(item["path"], ensure_ascii=False),
                        "key": _path_key(item["path"]),
                    },
                )

            schema_updates = 0
            for doc in docs:
                original_id = int(doc["classification_id"])
                final_id = merge_targets.get(original_id, original_id)
                target = by_id[final_id]
                target_path = desired_paths[final_id]
                updated, changed = _rewrite_schema_org_classification_terms(
                    doc.get("schema_org"),
                    target_ddc=str(target.get("ddc") or ""),
                    target_path_parts=target_path,
                )
                if not changed:
                    continue
                conn.execute(
                    text("UPDATE metadata SET schema_org=CAST(:schema AS JSON) WHERE md5=:md5"),
                    {
                        "md5": doc["md5"],
                        "schema": json.dumps(updated, ensure_ascii=False),
                    },
                )
                schema_updates += 1

        return available_payload(
            config_source=source,
            applied=True,
            change_set_hash=requested_hash,
            summary={**plan["summary"], "schema_org_updates": schema_updates},
        )
    finally:
        dispose_runtime_engine(engine)


def list_classification_documents(
    classification_ids: list[int], *, offset: int = 0, limit: int = 10
) -> dict[str, Any]:
    ids = sorted({_valid_id(item, "classification_id") for item in classification_ids})
    if not ids or len(ids) > 100:
        raise ValueError("classification_ids must contain 1 to 100 IDs")
    offset = max(0, int(offset))
    limit = max(1, min(50, int(limit)))
    engine, source = create_runtime_engine()
    try:
        with engine.connect() as conn:
            total = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM metadata WHERE classification_id=ANY(:ids)"),
                    {"ids": ids},
                ).scalar()
                or 0
            )
            rows = conn.execute(
                text(
                    """
                    SELECT d.md5,d.language,d.ya_path,d.mime_type,d.full,
                           d.sharing_restricted,
                           COALESCE(m.schema_org->>'name',m.schema_org->>'headline',d.ya_path,d.md5) title,
                           NULLIF(m.schema_org->>'numberOfPages','') page_count
                    FROM metadata m JOIN document d ON d.md5=m.md5
                    WHERE m.classification_id=ANY(:ids)
                    ORDER BY LOWER(COALESCE(m.schema_org->>'name',m.schema_org->>'headline',d.ya_path,d.md5)),d.md5
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {"ids": ids, "limit": limit, "offset": offset},
            ).mappings().all()
        items = []
        for row in rows:
            raw_pages = str(row.get("page_count") or "")
            items.append(
                {
                    "md5": str(row.get("md5") or ""),
                    "title": str(row.get("title") or ""),
                    "source_name": PurePosixPath(str(row.get("ya_path") or "")).name,
                    "language": str(row.get("language") or ""),
                    "mime_type": str(row.get("mime_type") or ""),
                    "page_count": int(raw_pages) if raw_pages.isdigit() else None,
                    "full": row.get("full"),
                    "sharing_restricted": row.get("sharing_restricted"),
                }
            )
        return available_payload(
            config_source=source,
            classification_ids=ids,
            offset=offset,
            limit=limit,
            total=total,
            has_more=offset + len(items) < total,
            items=items,
        )
    finally:
        dispose_runtime_engine(engine)


__all__ = [
    "StaleTaxonomyError",
    "apply_classification_change_set",
    "list_classification_documents",
    "preview_classification_change_set",
]
