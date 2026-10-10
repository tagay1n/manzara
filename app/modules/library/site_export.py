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


@dataclass(frozen=True)
class PublishedBundle:
    path: Path
    manifest: dict[str, Any]
    sha256: str


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


def _jsonl_bytes(records: Sequence[Mapping[str, Any]], should_stop: Callable[[], bool]) -> bytes:
    payloads = []
    for record in records:
        if should_stop():
            raise ExportStopped("Static Library export stopped during bundle preparation")
        payloads.append(_json_bytes(dict(record)))
    return b"".join(payloads)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_deterministic_tar_gz(
    path: Path, files: Mapping[str, bytes], order: Sequence[str], should_stop: Callable[[], bool]
) -> None:
    with path.open("wb") as raw_handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_handle, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as archive:
                for name in order:
                    if should_stop():
                        raise ExportStopped("Static Library export stopped during archive preparation")
                    payload = files[name]
                    info = tarfile.TarInfo(name=name)
                    info.size = len(payload)
                    info.mtime = 0
                    info.mode = 0o644
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    archive.addfile(info, fileobj=_BytesReader(payload, should_stop))


class _BytesReader:
    """Minimal file object accepted by tarfile without another dependency."""

    def __init__(self, value: bytes, should_stop: Callable[[], bool]) -> None:
        self._value = value
        self._offset = 0
        self._should_stop = should_stop

    def read(self, size: int = -1) -> bytes:
        if self._should_stop():
            raise ExportStopped("Static Library export stopped during archive preparation")
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
    should_stop: Callable[[], bool] = lambda: False,
) -> PublishedBundle:
    """Verify the staged bundle, then atomically replace only the published file."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise RuntimeError(f"Export destination must not be a symlink: {destination}")
    if destination.exists() and not destination.is_dir():
        raise RuntimeError(f"Export destination must be a directory: {destination}")
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        record_sets: dict[str, Sequence[Mapping[str, Any]]] = {
            "documents.jsonl": export.documents,
            "entities.jsonl": export.entities,
            "collections.jsonl": export.collections,
            "classifications.jsonl": export.classifications,
            "redirects.jsonl": export.redirects,
        }
        payloads = {name: _jsonl_bytes(records, should_stop) for name, records in record_sets.items()}
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
            should_stop,
        )
        verified = verify_export_bundle(bundle, should_stop=should_stop)
        digest = hashlib.sha256()
        with bundle.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                if should_stop():
                    raise ExportStopped("Static Library export stopped before publication")
                digest.update(chunk)
        if should_stop():
            raise ExportStopped("Static Library export stopped before publication")
        destination.mkdir(exist_ok=True)
        published = destination / EXPORT_BUNDLE_NAME
        os.replace(bundle, published)
        return PublishedBundle(published, verified, digest.hexdigest())
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def verify_export_bundle(path: Path, *, should_stop: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    """Verify a bundle without a database, storage client or static-site build."""
    identifiers: dict[str, set[str]] = {}
    references: dict[str, set[str]] = {name: set() for name in EXPORT_FILES}
    paths: set[str] = set()
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        if [item.name for item in members] != ["manifest.json", *EXPORT_FILES] or not all(item.isfile() for item in members):
            raise ValueError("Invalid Library export archive members")
        with archive.extractfile("manifest.json") as member:
            manifest = json.load(member)
        if (not isinstance(manifest, dict) or manifest.get("format") != EXPORT_FORMAT
                or type(manifest.get("version")) is not int or manifest["version"] != EXPORT_VERSION
                or manifest.get("metadata_contract") != CONTRACT_VERSION
                or not isinstance(manifest.get("files"), dict) or set(manifest["files"]) != set(EXPORT_FILES)):
            raise ValueError("Unsupported Library export manifest")
        for name in EXPORT_FILES:
            identifiers[name] = set()
            digest, count = hashlib.sha256(), 0
            with archive.extractfile(name) as member:
                for line in member:
                    if should_stop():
                        raise ExportStopped("Static Library export stopped during bundle verification")
                    digest.update(line)
                    record = json.loads(line)
                    _verify_record(name, record, identifiers[name], references, paths)
                    count += 1
            declared = manifest["files"][name]
            if (not isinstance(declared, dict) or type(declared.get("records")) is not int
                    or declared != {"records": count, "sha256": digest.hexdigest()}):
                raise ValueError(f"Library export checksum/count mismatch: {name}")
    revision_input = "\n".join(f"{name}:{manifest['files'][name]['sha256']}" for name in EXPORT_FILES).encode("utf-8")
    if manifest["revision"] != f"sha256:{_sha256(revision_input)}":
        raise ValueError("Library export revision mismatch")
    for name, required in references.items():
        if required - identifiers[name]:
            raise ValueError(f"Library export contains dangling references to {name}")
    return manifest


def _verify_record(name, record, identifiers, references, paths):
    if not isinstance(record, dict):
        raise ValueError(f"Library export record must be an object: {name}")
    if name == "redirects.jsonl":
        raise ValueError("Library export redirects are not implemented")
    identifier, path = record.get("id"), record.get("path")
    if not isinstance(identifier, str) or not identifier or identifier in identifiers:
        raise ValueError(f"Duplicate/invalid Library export ID: {name}")
    if not isinstance(path, str) or not path.startswith("/") or path in paths:
        raise ValueError(f"Duplicate/invalid Library export path: {name}")
    identifiers.add(identifier)
    paths.add(path)
    if name == "documents.jsonl":
        if metadata_contract_issues(record["work"]):
            raise ValueError(f"Invalid exported metadata: {identifier}")
        relations = record["relations"]
        entities = {credit["entity_id"] for credit in relations["contributors"] if credit["entity_id"]}
        if relations["publisher_id"]:
            entities.add(relations["publisher_id"])
        references["entities.jsonl"].update(entities)
        for relation, filename in (("collection_id", "collections.jsonl"), ("classification_id", "classifications.jsonl")):
            if relations[relation]:
                references[filename].add(relations[relation])
    if name in {"collections.jsonl", "classifications.jsonl"}:
        documents = record["document_ids"]
        if (len(set(documents)) != len(documents) or type(record["document_count"]) is not int
                or record["document_count"] != len(documents)):
            raise ValueError(f"Invalid Library export membership: {identifier}")
        references["documents.jsonl"].update(documents)


__all__ = [
    "EXPORT_BUNDLE_NAME",
    "EXPORT_FORMAT",
    "EXPORT_VERSION",
    "ExportStorage",
    "ExportStopped",
    "LibraryExport",
    "build_library_export",
    "write_export_bundle",
    "verify_export_bundle",
]
