"""Stable, public, database-independent Library export contract."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import unicodedata
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from app.document_storage import object_url, parse_object_url
from app.modules.library.metadata_contract import (
    CONTRACT_VERSION,
    metadata_contract_issues,
)
from app.modules.library.previews import (
    PREVIEW_RECIPE_VERSION,
    preview_object_key,
    preview_pages_from_row,
)
from app.modules.library.runtime.metadata.fields import extract_publish_year


EXPORT_FORMAT = "manzara-library-export"
EXPORT_VERSION = 1
EXPORT_BUNDLE_NAME = "library-export-v1.tar.gz"
EXPORT_FILES = (
    "documents.jsonl",
    "entities.jsonl",
    "collections.jsonl",
    "classifications.jsonl",
    "redirects.jsonl",
)
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_SLUG_SPACES_RE = re.compile(r"[\s_]+", flags=re.UNICODE)
_SLUG_CLEAN_RE = re.compile(r"[^\w-]+", flags=re.UNICODE)
_CONTRIBUTOR_PROPERTIES = ("author", "editor", "translator", "illustrator")


@dataclass(frozen=True)
class ExportStorage:
    """Public storage locations allowed to appear in a static export."""

    endpoint_url: str
    public_document_bucket: str
    public_preview_bucket: str
    public_content_bucket: str = ""


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


def _entity_path(entity_type: str, name: str, canonical_id: int) -> str:
    segment = "authors" if entity_type == "personality" else "publishers"
    return f"/{segment}/{_slug(name, segment[:-1])}--{canonical_id}/"


def _public_object_url(
    raw_url: Any,
    *,
    storage: ExportStorage,
    bucket: str,
) -> str | None:
    value = str(raw_url or "").strip()
    if not value or value.startswith("enc:") or urlparse(value).query:
        return None
    location = parse_object_url(value, storage.endpoint_url)
    if not location or location[0] != bucket:
        return None
    return object_url(storage.endpoint_url, location[0], location[1])


def _exclusion_reason(row: Mapping[str, Any], storage: ExportStorage) -> str | None:
    digest = str(row.get("md5") or "").strip().lower()
    if not _MD5_RE.fullmatch(digest):
        return "invalid_md5"
    if row.get("has_active_corruption") is True:
        return "active_corruption"
    if row.get("full") is not True:
        return "not_full"
    if row.get("sharing_restricted") is not False:
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


def _alias_index(
    aliases: Iterable[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, set[str]]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    names: dict[str, set[str]] = {}
    for raw in aliases:
        row = dict(raw)
        entity_type = str(row.get("entity_type") or "").strip()
        raw_name = str(row.get("raw_name") or "").strip()
        canonical_id = int(row.get("canonical_id") or 0)
        if (
            entity_type not in {"personality", "publisher"}
            or not raw_name
            or canonical_id <= 0
            or row.get("decision_status") != "linked"
            or row.get("canonical_status") != "active"
        ):
            continue
        entity_id = f"{entity_type}:{canonical_id}"
        row["entity_id"] = entity_id
        index[(entity_type, raw_name)] = row
        names.setdefault(entity_id, set()).add(raw_name)
    return index, names


def _entity_name(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    entity_type = str(value.get("@type") or "")
    name = str(value.get("name") or "").strip()
    if entity_type not in {"Person", "Organization"} or not name:
        return None
    return entity_type, name


def _contributor_relations(
    work: Mapping[str, Any], alias_index: Mapping[tuple[str, str], Mapping[str, Any]]
) -> list[dict[str, Any]]:
    relations: list[dict[str, Any]] = []
    for property_name in _CONTRIBUTOR_PROPERTIES:
        for item in _as_list(work.get(property_name)):
            entity = _entity_name(item)
            if not entity:
                continue
            _schema_type, source_name = entity
            alias = alias_index.get(("personality", source_name))
            relations.append(
                {
                    "entity_id": alias.get("entity_id") if alias else None,
                    "property": property_name,
                    "role_name": None,
                    "display_name": (
                        str(alias.get("display_name") or source_name)
                        if alias
                        else source_name
                    ),
                    "source_name": source_name,
                }
            )
    for item in _as_list(work.get("contributor")):
        role_name: str | None = None
        entity_value = item
        if isinstance(item, Mapping) and item.get("@type") == "Role":
            role_name = str(item.get("roleName") or "").strip() or None
            entity_value = item.get("contributor")
        entity = _entity_name(entity_value)
        if not entity:
            continue
        _schema_type, source_name = entity
        alias = alias_index.get(("personality", source_name))
        relations.append(
            {
                "entity_id": alias.get("entity_id") if alias else None,
                "property": "contributor",
                "role_name": role_name,
                "display_name": (
                    str(alias.get("display_name") or source_name)
                    if alias
                    else source_name
                ),
                "source_name": source_name,
            }
        )
    return relations


def _publisher_relation(
    work: Mapping[str, Any], alias_index: Mapping[tuple[str, str], Mapping[str, Any]]
) -> tuple[str | None, str | None]:
    publisher = _entity_name(work.get("publisher"))
    if not publisher:
        return None, None
    _schema_type, source_name = publisher
    alias = alias_index.get(("publisher", source_name))
    return (str(alias.get("entity_id")) if alias else None), source_name


def _preview(row: Mapping[str, Any], storage: ExportStorage) -> dict[str, Any] | None:
    if (
        row.get("preview_recipe_version") != PREVIEW_RECIPE_VERSION
        or row.get("preview_status") != "ready"
        or not storage.public_preview_bucket
    ):
        return None
    preview_row = {
        "md5": row.get("md5"),
        "first_preview_page": row.get("first_preview_page"),
        "second_preview_page": row.get("second_preview_page"),
        "last_preview_page": row.get("last_preview_page"),
    }
    pages: list[dict[str, Any]] = []
    for page in preview_pages_from_row(preview_row):
        pages.append(
            {
                "role": page.role,
                "page_number": page.page_number,
                "small_url": object_url(
                    storage.endpoint_url,
                    storage.public_preview_bucket,
                    preview_object_key(str(row["md5"]), page.object_alias, "small"),
                ),
                "large_url": object_url(
                    storage.endpoint_url,
                    storage.public_preview_bucket,
                    preview_object_key(str(row["md5"]), page.object_alias, "large"),
                ),
            }
        )
    if not pages:
        return None
    count = row.get("source_page_count")
    return {
        "source_page_count": int(count) if count is not None else None,
        "pages": pages,
    }


def _language_facets(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _build_document(
    row: Mapping[str, Any],
    *,
    alias_index: Mapping[tuple[str, str], Mapping[str, Any]],
    storage: ExportStorage,
) -> tuple[dict[str, Any], set[str]]:
    digest = str(row["md5"]).lower()
    work = deepcopy(_as_mapping(row.get("schema_org")))
    title = str(work.get("name") or "").strip()
    contributors = _contributor_relations(work, alias_index)
    publisher_id, _publisher_source = _publisher_relation(work, alias_index)
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
    if storage.public_content_bucket:
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
    aliases: Iterable[Mapping[str, Any]],
    storage: ExportStorage,
    should_stop: Callable[[], bool] = lambda: False,
) -> LibraryExport:
    """Transform database-shaped rows into the stable public export domain."""
    alias_rows = [dict(row) for row in aliases]
    alias_index, alias_names = _alias_index(alias_rows)
    exclusions: Counter[str] = Counter()
    documents: list[dict[str, Any]] = []
    used_entity_ids: set[str] = set()
    entity_document_counts: Counter[str] = Counter()
    collection_rows: dict[int, dict[str, Any]] = {}
    classification_rows: dict[int, dict[str, Any]] = {}

    for row_value in candidates:
        if should_stop():
            raise ExportStopped("Static Library export stopped before publication")
        row = dict(row_value)
        reason = _exclusion_reason(row, storage)
        if reason:
            exclusions[reason] += 1
            continue
        document, used = _build_document(
            row, alias_index=alias_index, storage=storage
        )
        documents.append(document)
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
    by_entity_id = {
        str(row.get("entity_id")): row
        for row in alias_index.values()
        if row.get("entity_id")
    }
    entities: list[dict[str, Any]] = []
    for entity_id in sorted(used_entity_ids):
        source = by_entity_id[entity_id]
        entity_type = str(source["entity_type"])
        canonical_id = int(source["canonical_id"])
        name = str(source.get("display_name") or "").strip()
        entities.append(
            {
                "id": entity_id,
                "kind": "person" if entity_type == "personality" else "organization",
                "name": name,
                "path": _entity_path(entity_type, name, canonical_id),
                "aliases": sorted(alias_names.get(entity_id, set()), key=str.casefold),
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
        entities=_normalize_public_text(entities),
        collections=_normalize_public_text(collections),
        classifications=_normalize_public_text(classifications),
        redirects=[],
        exclusions=dict(sorted(exclusions.items())),
    )


def _json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _jsonl_bytes(records: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(_json_bytes(dict(record)) for record in records)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_deterministic_tar_gz(
    path: Path, files: Mapping[str, bytes], order: Sequence[str]
) -> None:
    with path.open("wb") as raw_handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_handle, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as archive:
                for name in order:
                    payload = files[name]
                    info = tarfile.TarInfo(name=name)
                    info.size = len(payload)
                    info.mtime = 0
                    info.mode = 0o644
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    archive.addfile(info, fileobj=_BytesReader(payload))


class _BytesReader:
    """Minimal file object accepted by tarfile without another dependency."""

    def __init__(self, value: bytes) -> None:
        self._value = value
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._value) - self._offset
        result = self._value[self._offset : self._offset + size]
        self._offset += len(result)
        return result


def write_export_bundle(
    export: LibraryExport,
    *,
    destination: Path,
    generated_at: str | None = None,
) -> Path:
    """Atomically publish one checksummed export bundle to a new directory."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Export destination already exists: {destination}")
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        record_sets: dict[str, Sequence[Mapping[str, Any]]] = {
            "documents.jsonl": export.documents,
            "entities.jsonl": export.entities,
            "collections.jsonl": export.collections,
            "classifications.jsonl": export.classifications,
            "redirects.jsonl": export.redirects,
        }
        payloads = {name: _jsonl_bytes(records) for name, records in record_sets.items()}
        file_manifest = {
            name: {
                "records": len(record_sets[name]),
                "sha256": _sha256(payloads[name]),
            }
            for name in EXPORT_FILES
        }
        revision_input = "\n".join(
            f"{name}:{file_manifest[name]['sha256']}" for name in EXPORT_FILES
        ).encode("utf-8")
        manifest = {
            "format": EXPORT_FORMAT,
            "version": EXPORT_VERSION,
            "revision": f"sha256:{_sha256(revision_input)}",
            "generated_at": generated_at or _generated_at(),
            "metadata_contract": CONTRACT_VERSION,
            "files": file_manifest,
            "statistics": {
                "documents_published": len(export.documents),
                "documents_excluded": sum(export.exclusions.values()),
                "documents_with_previews": sum(
                    "preview" in item for item in export.documents
                ),
                "exclusion_reasons": export.exclusions,
            },
        }
        payloads["manifest.json"] = _json_bytes(manifest)
        bundle = stage / EXPORT_BUNDLE_NAME
        _write_deterministic_tar_gz(
            bundle,
            payloads,
            ("manifest.json", *EXPORT_FILES),
        )
        os.replace(stage, destination)
        return destination / EXPORT_BUNDLE_NAME
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


__all__ = [
    "EXPORT_BUNDLE_NAME",
    "EXPORT_FORMAT",
    "EXPORT_VERSION",
    "ExportStorage",
    "ExportStopped",
    "LibraryExport",
    "build_library_export",
    "write_export_bundle",
]
