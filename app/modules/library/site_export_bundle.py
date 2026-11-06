"""Deterministic Library export archives, verification, and atomic publication."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.catalog.schema_org import CONTRACT_VERSION, metadata_contract_issues
from app.modules.library.site_export import ExportStopped, LibraryExport


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


@dataclass(frozen=True)
class PublishedBundle:
    path: Path
    manifest: dict[str, Any]
    sha256: str


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
