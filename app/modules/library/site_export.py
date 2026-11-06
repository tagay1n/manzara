"""Stable, public, database-independent Library export contract."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlparse

from app.document_storage import object_url, parse_object_url
from app.catalog.schema_org import (
    metadata_contract_issues,
)
from app.modules.library.runtime.metadata.fields import extract_publish_year


_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_SLUG_SPACES_RE = re.compile(r"[\s_]+", flags=re.UNICODE)
_SLUG_CLEAN_RE = re.compile(r"[^\w-]+", flags=re.UNICODE)


@dataclass(frozen=True)
class ExportStorage:
    """Public storage locations allowed to appear in a static export."""

    endpoint_url: str
    public_document_bucket: str
    public_preview_bucket: str
    public_content_bucket: str = ""

    def __post_init__(self):
        endpoint = urlparse(self.endpoint_url)
        if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
                or endpoint.username is not None or endpoint.password is not None
                or endpoint.query or endpoint.fragment or endpoint.path not in {"", "/"}):
            raise ValueError("Static Library export requires a public S3 endpoint without credentials or a path")
        if not self.public_document_bucket:
            raise ValueError("Static Library export requires documents.primary_storage.bucket.public")
        for bucket in (self.public_document_bucket, self.public_preview_bucket, self.public_content_bucket):
            if bucket and ("/" in bucket or "\\" in bucket or bucket.strip() != bucket):
                raise ValueError("Invalid public export bucket")


@dataclass
class LibraryExport:
    """In-memory, version-independent result used by the bundle writer."""

    documents: list[dict[str, Any]] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    collections: list[dict[str, Any]] = field(default_factory=list)
    classifications: list[dict[str, Any]] = field(default_factory=list)
    redirects: list[dict[str, Any]] = field(default_factory=list)
    exclusions: dict[str, int] = field(default_factory=dict)


class ExportStopped(RuntimeError):
    """Raised at a safe document boundary before a bundle is published."""


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return [value.strip()]
        if isinstance(decoded, list):
            return [str(item).strip() for item in decoded if str(item).strip()]
    return []


def _slug(value: Any, fallback: str) -> str:
    normalized = unicodedata.normalize("NFC", str(value or "")).casefold().strip()
    normalized = _SLUG_SPACES_RE.sub("-", normalized)
    normalized = _SLUG_CLEAN_RE.sub("", normalized).strip("-")
    return normalized or fallback


def _normalize_public_text(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_normalize_public_text(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_public_text(item) for key, item in value.items()}
    return value


def _entity_path(entity_type: str, name: str, catalog_id: int) -> str:
    segment = "authors" if entity_type == "personality" else "publishers"
    return f"/{segment}/{_slug(name, segment[:-1])}--{catalog_id}/"


def _public_object_url(
    raw_url: Any,
    *,
    storage: ExportStorage,
    bucket: str,
) -> str | None:
    value = str(raw_url or "").strip()
    parsed = urlparse(value)
    if (not value or parsed.query or parsed.fragment
            or parsed.username is not None or parsed.password is not None):
        return None
    location = parse_object_url(value, storage.endpoint_url)
    if not location or location[0] != bucket:
        return None
    return object_url(storage.endpoint_url, location[0], location[1])


def _public_reference_url(value: Any, storage: ExportStorage) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.query
            or parsed.username is not None or parsed.password is not None):
        return False
    if parsed.netloc == urlparse(storage.endpoint_url).netloc:
        location = parse_object_url(value, storage.endpoint_url)
        return bool(location and location[0] in {
            storage.public_document_bucket, storage.public_preview_bucket, storage.public_content_bucket,
        })
    return True


def _public_metadata(value: Any, storage: ExportStorage) -> Any:
    """Drop unsafe URL fields without exporting storage/source evidence."""
    if isinstance(value, list):
        return [_public_metadata(item, storage) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == "url" or (key == "inDefinedTermSet" and isinstance(item, str)):
            urls = [url for url in _as_list(item) if _public_reference_url(url, storage)]
            if urls or isinstance(item, list):
                result[key] = urls if isinstance(item, list) else urls[0]
        else:
            result[key] = _public_metadata(item, storage)
    return result


def _exclusion_reason(row: Mapping[str, Any], storage: ExportStorage) -> str | None:
    digest = str(row.get("md5") or "").strip().lower()
    if not _MD5_RE.fullmatch(digest):
        return "invalid_md5"
    if row.get("inclusion") != "included":
        return "not_included"
    if row.get("selected") is not True:
        return "not_selected"
    if row.get("has_active_cleanup") is True:
        return "active_cleanup"
    if row.get("complete") is not True:
        return "not_full"
    if row.get("restricted") is not False:
        return "sharing_restricted"
    if row.get("primary_storage_size") is None or row.get(
        "primary_storage_verified_at"
    ) is None:
        return "unverified_storage"
    if not _public_object_url(
        row.get("document_url"),
        storage=storage,
        bucket=storage.public_document_bucket,
    ):
        return "not_public_storage"
    schema_org = _as_mapping(row.get("schema_org"))
    if not schema_org or metadata_contract_issues(schema_org):
        return "invalid_metadata"
    return None


def _preview(row: Mapping[str, Any], storage: ExportStorage) -> dict[str, Any] | None:
    preview = row.get("preview")
    if not preview or preview["private"] is not False or not storage.public_preview_bucket:
        return None
    pages = []
    roles, numbers = set(), set()
    count = preview["source_page_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(f"Invalid preview page count for {row['md5']}")
    for page in preview["pages"]:
        role, number = page["role"], page["page_number"]
        if (role not in {"first", "second", "last"} or role in roles or number in numbers
                or isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= count):
            raise ValueError(f"Invalid preview page selection for {row['md5']}")
        urls = {}
        for variant in ("small", "large"):
            key = page[f"{variant}_key"]
            if (not isinstance(key, str) or not key.strip() or key.startswith("/")
                    or "\\" in key or any(part in {".", "..", ""} for part in key.split("/"))):
                raise ValueError(f"Invalid preview object key for {row['md5']}")
            urls[f"{variant}_url"] = object_url(storage.endpoint_url, storage.public_preview_bucket, key)
        pages.append({"role": role, "page_number": number, **urls})
        roles.add(role)
        numbers.add(number)
    return {"source_page_count": count, "pages": pages} if pages else None


def _language_facets(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _build_document(
    row: Mapping[str, Any],
    *,
    storage: ExportStorage,
) -> tuple[dict[str, Any], set[str]]:
    digest = str(row["md5"]).lower()
    work = deepcopy(_as_mapping(row.get("schema_org")))
    title = str(work.get("name") or "").strip()
    contributors = []
    publisher_id = None
    for credit in row["contributions"]:
        entity_type = "publisher" if credit["role"] == "publisher" else "personality"
        entity_id = f"{entity_type}:{credit['entity_id']}" if credit["entity_id"] is not None else None
        if credit["role"] == "publisher":
            publisher_id = entity_id
        else:
            contributors.append({"entity_id": entity_id, "property": credit["role"],
                "role_name": credit["role_name"], "source_name": credit["raw_name"],
                "display_name": credit["display_name"]})
    used_entities = {
        str(item["entity_id"])
        for item in contributors
        if item.get("entity_id")
    }
    if publisher_id:
        used_entities.add(publisher_id)

    collection_id = int(row.get("collection_id") or 0)
    if not row.get("collection_include"):
        collection_id = 0
    classification_id = int(row.get("classification_id") or 0)
    content_url = None
    if storage.public_content_bucket and row.get("content_verified_at") is not None:
        content_url = _public_object_url(
            row.get("content_url"),
            storage=storage,
            bucket=storage.public_content_bucket,
        )
    genres = [
        str(item).strip()
        for item in _as_list(work.get("genre"))
        if isinstance(item, str) and item.strip()
    ]
    record: dict[str, Any] = {
        "id": f"document:{digest}",
        "md5": digest,
        "path": f"/books/{_slug(title, 'document')}--{digest[:8]}/",
        "work": work,
        "file": {
            "mime_type": str(row.get("mime_type") or "application/octet-stream"),
            "size": int(row["primary_storage_size"]),
            "download_url": _public_object_url(
                row.get("document_url"),
                storage=storage,
                bucket=storage.public_document_bucket,
            ),
            "content_url": content_url,
        },
        "relations": {
            "contributors": contributors,
            "publisher_id": publisher_id,
            "collection_id": f"collection:{collection_id}" if collection_id else None,
            "classification_id": (
                f"classification:{classification_id}" if classification_id else None
            ),
        },
        "facets": {
            "languages": _language_facets(work.get("inLanguage")),
            "genres": genres,
            "publication_year": extract_publish_year(work),
            "work_type": str(work.get("@type") or ""),
        },
    }
    preview = _preview(row, storage)
    if preview:
        record["preview"] = preview
    return record, used_entities


def build_library_export(
    candidates: Iterable[Mapping[str, Any]],
    *,
    entities: Iterable[Mapping[str, Any]],
    storage: ExportStorage,
    should_stop: Callable[[], bool] = lambda: False,
    log: Callable[[str], None] = lambda _message: None,
    progress: Callable[..., None] = lambda *_args, **_kwargs: None,
) -> LibraryExport:
    """Transform database-shaped rows into the stable public export domain."""
    entity_rows = [dict(row) for row in entities]
    exclusions: Counter[str] = Counter()
    documents: list[dict[str, Any]] = []
    used_entity_ids: set[str] = set()
    entity_document_counts: Counter[str] = Counter()
    collection_rows: dict[int, dict[str, Any]] = {}
    classification_rows: dict[int, dict[str, Any]] = {}

    for current, row_value in enumerate(candidates, 1):
        if should_stop():
            raise ExportStopped("Static Library export stopped before publication")
        row = dict(row_value)
        row["schema_org"] = _public_metadata(_as_mapping(row.get("schema_org")), storage)
        reason = _exclusion_reason(row, storage)
        if reason:
            exclusions[reason] += 1
            log(f"static library export: exclude md5={row.get('md5')} reason={reason}")
            progress({"phase": "processing", "current": current, "published": len(documents), "excluded": sum(exclusions.values())})
            continue
        document, used = _build_document(row, storage=storage)
        documents.append(document)
        log(f"static library export: include md5={row['md5']}")
        progress({"phase": "processing", "current": current, "published": len(documents), "excluded": sum(exclusions.values())})
        used_entity_ids.update(used)
        entity_document_counts.update(used)
        document_id = str(document["id"])

        collection_id = int(row.get("collection_id") or 0)
        if collection_id and row.get("collection_include"):
            item = collection_rows.setdefault(
                collection_id,
                {
                    "id": f"collection:{collection_id}",
                    "name": str(row.get("collection_title") or "").strip(),
                    "document_ids": [],
                },
            )
            item["document_ids"].append(document_id)

        classification_id = int(row.get("classification_id") or 0)
        if classification_id:
            item = classification_rows.setdefault(
                classification_id,
                {
                    "id": f"classification:{classification_id}",
                    "ddc": str(row.get("ddc") or "").strip(),
                    "labels": {
                        "en": _json_list(row.get("path_en")),
                    },
                    "document_ids": [],
                },
            )
            path_tt = _json_list(row.get("path_tt"))
            if path_tt:
                item["labels"]["tt"] = path_tt
            item["document_ids"].append(document_id)

    documents.sort(key=lambda item: str(item["id"]))
    by_entity_id = {}
    alias_names: dict[str, set[str]] = {}
    for row in entity_rows:
        entity_id = f"{row['entity_type']}:{row['entity_id']}"
        by_entity_id[entity_id] = row
        alias_names.setdefault(entity_id, set()).add(row["alias_name"])
    exported_entities: list[dict[str, Any]] = []
    for entity_id in sorted(used_entity_ids):
        source = by_entity_id[entity_id]
        entity_type = str(source["entity_type"])
        catalog_id = int(source["entity_id"])
        name = str(source.get("display_name") or "").strip()
        exported_entities.append(
            {
                "id": entity_id,
                "kind": source["kind"],
                "name": name,
                "path": _entity_path(entity_type, name, catalog_id),
                "aliases": sorted(alias_names.get(entity_id, set()), key=lambda name: (name.casefold(), name)),
                "document_count": entity_document_counts[entity_id],
            }
        )

    collections: list[dict[str, Any]] = []
    for collection_id, item in sorted(collection_rows.items()):
        item["document_ids"].sort()
        item["document_count"] = len(item["document_ids"])
        item["path"] = (
            f"/collections/{_slug(item['name'], 'collection')}--{collection_id}/"
        )
        collections.append(item)

    classifications: list[dict[str, Any]] = []
    for classification_id, item in sorted(classification_rows.items()):
        item["document_ids"].sort()
        item["document_count"] = len(item["document_ids"])
        path_parts = item["labels"].get("en") or [item["ddc"]]
        path_slug = "/".join(_slug(part, "category") for part in path_parts)
        item["path"] = f"/categories/{path_slug}--{classification_id}/"
        classifications.append(item)

    return LibraryExport(
        documents=_normalize_public_text(documents),
        entities=_normalize_public_text(exported_entities),
        collections=_normalize_public_text(collections),
        classifications=_normalize_public_text(classifications),
        redirects=[],
        exclusions=dict(sorted(exclusions.items())),
    )


__all__ = ["ExportStorage", "ExportStopped", "LibraryExport", "build_library_export"]
