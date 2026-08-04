"""Shared configuration and integrity helpers for primary document storage."""

from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from urllib.parse import quote, unquote, urlparse


DEFAULT_S3_ENDPOINT = "https://storage.yandexcloud.net"
DEFAULT_S3_REGION = "ru-central1"
DEFAULT_MULTIPART_CHUNK_SIZE = 8 * 1024 * 1024
_PARTIAL_SUFFIXES = {".crdownload", ".download", ".part", ".partial", ".tmp"}
_SAFE_EXTENSION = re.compile(r"^\.[a-z0-9]{1,12}$")
_MIME_EXTENSIONS = {
    "application/epub+zip": ".epub",
    "application/msword": ".doc",
    "application/pdf": ".pdf",
    "application/rtf": ".rtf",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/x-fictionbook+xml": ".fb2",
    "image/vnd.djvu": ".djvu",
    "text/html": ".html",
    "text/plain": ".txt",
    "text/rtf": ".rtf",
}


@dataclass(frozen=True)
class S3ConnectionSettings:
    """Credentials and endpoint for one S3-compatible service."""

    endpoint_url: str
    region_name: str
    access_key_id: str
    secret_access_key: str


@dataclass(frozen=True)
class DocumentStorageSettings:
    """Resolved source, primary, and legacy document storage settings."""

    cache_path: Path
    source_path: str
    restricted_path: str
    primary: S3ConnectionSettings
    legacy: S3ConnectionSettings
    public_bucket: str
    private_bucket: str
    legacy_public_bucket: str
    legacy_private_bucket: str
    upstream_bucket: str
    encryption_key: str
    yadisk_token: str = ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _required(mapping: Mapping[str, Any], key: str, path: str) -> str:
    value = str(mapping.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required config value: {path}.{key}")
    return value


def load_document_storage_settings(payload: Mapping[str, Any]) -> DocumentStorageSettings:
    """Parse the single supported document-storage configuration contract."""
    documents = _mapping(payload.get("documents"))
    yandex = _mapping(payload.get("yandex"))
    disk = _mapping(yandex.get("disk"))
    disk_documents = _mapping(disk.get("documents"))
    legacy_cloud = _mapping(yandex.get("cloud"))
    legacy_buckets = _mapping(legacy_cloud.get("bucket"))
    primary_storage = _mapping(documents.get("primary_storage"))
    primary_buckets = _mapping(primary_storage.get("bucket"))
    return DocumentStorageSettings(
        cache_path=Path(_required(documents, "cache_path", "documents")).expanduser(),
        source_path=_required(disk_documents, "source_path", "yandex.disk.documents"),
        restricted_path=_required(
            disk_documents, "restricted_path", "yandex.disk.documents"
        ),
        primary=S3ConnectionSettings(
            endpoint_url=_required(
                primary_storage, "endpoint_url", "documents.primary_storage"
            ).rstrip("/"),
            region_name=_required(
                primary_storage, "region_name", "documents.primary_storage"
            ),
            access_key_id=_required(
                primary_storage, "access_key_id", "documents.primary_storage"
            ),
            secret_access_key=_required(
                primary_storage, "secret_access_key", "documents.primary_storage"
            ),
        ),
        legacy=S3ConnectionSettings(
            endpoint_url=str(
                legacy_cloud.get("endpoint_url") or DEFAULT_S3_ENDPOINT
            ).rstrip("/"),
            region_name=str(
                legacy_cloud.get("region_name") or DEFAULT_S3_REGION
            ).strip(),
            access_key_id=_required(
                legacy_cloud, "aws_access_key_id", "yandex.cloud"
            ),
            secret_access_key=_required(
                legacy_cloud, "aws_secret_access_key", "yandex.cloud"
            ),
        ),
        public_bucket=_required(
            primary_buckets, "public", "documents.primary_storage.bucket"
        ),
        private_bucket=_required(
            primary_buckets, "private", "documents.primary_storage.bucket"
        ),
        legacy_public_bucket=_required(
            legacy_buckets, "document", "yandex.cloud.bucket"
        ),
        legacy_private_bucket=_required(
            legacy_buckets, "document_private", "yandex.cloud.bucket"
        ),
        upstream_bucket=_required(
            legacy_buckets, "upstream_metadata", "yandex.cloud.bucket"
        ),
        encryption_key=_required(payload, "encryption_key", "config"),
        yadisk_token=_required(disk, "oauth_token", "yandex.disk"),
    )


def calculate_md5(path: Path) -> str:
    """Return the MD5 content identity used by monocorpus."""
    digest = hashlib.md5()  # noqa: S324 - existing document identity is MD5.
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def calculate_multipart_etag(
    path: Path,
    *,
    chunk_size: int = DEFAULT_MULTIPART_CHUNK_SIZE,
) -> str:
    """Reproduce boto3's default multipart ETag for a local file."""
    part_digests: list[bytes] = []
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(int(chunk_size)), b""):
            part_digests.append(hashlib.md5(chunk).digest())  # noqa: S324
    if not part_digests:
        return hashlib.md5(b"").hexdigest()  # noqa: S324
    if len(part_digests) == 1:
        return part_digests[0].hex()
    combined = hashlib.md5(b"".join(part_digests)).hexdigest()  # noqa: S324
    return f"{combined}-{len(part_digests)}"


def calculate_integrity(
    path: Path,
    *,
    chunk_size: int = DEFAULT_MULTIPART_CHUNK_SIZE,
) -> tuple[str, str]:
    """Calculate content MD5 and boto3 multipart ETag in one file pass."""
    full_digest = hashlib.md5()  # noqa: S324 - existing document identity is MD5.
    part_digests: list[bytes] = []
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(int(chunk_size)), b""):
            full_digest.update(chunk)
            part_digests.append(hashlib.md5(chunk).digest())  # noqa: S324
    if not part_digests:
        multipart_etag = hashlib.md5(b"").hexdigest()  # noqa: S324
    elif len(part_digests) == 1:
        multipart_etag = part_digests[0].hex()
    else:
        combined = hashlib.md5(b"".join(part_digests)).hexdigest()  # noqa: S324
        multipart_etag = f"{combined}-{len(part_digests)}"
    return full_digest.hexdigest(), multipart_etag


def build_cache_index(cache_path: Path) -> dict[str, list[Path]]:
    """Index MD5-prefixed cache candidates with one directory scan."""
    index: dict[str, list[Path]] = {}
    if not cache_path.is_dir():
        return index
    for candidate in cache_path.iterdir():
        if not candidate.is_file() or candidate.suffix.lower() in _PARTIAL_SUFFIXES:
            continue
        digest = candidate.name.split(".", 1)[0].lower()
        if len(digest) != 32 or any(char not in "0123456789abcdef" for char in digest):
            continue
        index.setdefault(digest, []).append(candidate)
    for candidates in index.values():
        candidates.sort()
    return index


def find_valid_cache_entry(
    cache_index: Mapping[str, Iterable[Path]],
    md5: str,
) -> tuple[Path, str] | None:
    """Return a matching cache path and its multipart ETag."""
    digest = str(md5 or "").strip().lower()
    for candidate in cache_index.get(digest, []):
        actual_md5, multipart_etag = calculate_integrity(candidate)
        if actual_md5 == digest:
            return candidate, multipart_etag
    return None


def find_valid_cache_file(cache_path: Path, md5: str) -> Path | None:
    """Find a non-partial cache file whose bytes match the requested MD5."""
    digest = str(md5 or "").strip().lower()
    entry = find_valid_cache_entry(build_cache_index(cache_path), digest)
    return entry[0] if entry else None


def normalized_extension(source_path: str, mime_type: str | None) -> str:
    """Choose a safe stable extension from source path and normalized MIME."""
    suffix = PurePosixPath(str(source_path or "")).suffix.lower()
    if _SAFE_EXTENSION.fullmatch(suffix):
        return suffix
    normalized_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    mapped = _MIME_EXTENSIONS.get(normalized_mime)
    if mapped:
        return mapped
    guessed = mimetypes.guess_extension(normalized_mime, strict=False)
    if guessed and _SAFE_EXTENSION.fullmatch(guessed.lower()):
        return guessed.lower()
    return ".bin"


def document_object_key(md5: str, source_path: str, mime_type: str | None) -> str:
    """Return a flat content-addressed key for one document."""
    digest = str(md5 or "").strip().lower()
    if len(digest) != 32 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"Invalid document MD5: {md5!r}")
    return digest + normalized_extension(source_path, mime_type)


def object_url(endpoint_url: str, bucket: str, key: str) -> str:
    """Build a canonical path-style object URL."""
    encoded_key = "/".join(quote(part, safe="") for part in str(key).split("/"))
    return f"{str(endpoint_url).rstrip('/')}/{quote(str(bucket), safe='')}/{encoded_key}"


def parse_object_url(url: str, endpoint_url: str) -> tuple[str, str] | None:
    """Parse a path-style URL belonging to the configured S3 endpoint."""
    parsed = urlparse(str(url or ""))
    endpoint = urlparse(str(endpoint_url or ""))
    if parsed.scheme not in {"http", "https"} or parsed.netloc != endpoint.netloc:
        return None
    parts = parsed.path.lstrip("/").split("/", 1)
    if len(parts) != 2 or not all(parts):
        return None
    return unquote(parts[0]), "/".join(unquote(part) for part in parts[1].split("/"))


def resolve_document_download_url(
    *,
    document_url: str | None,
    fallback_url: str | None,
    encryption_key: str,
    endpoint_url: str,
    private_bucket: str,
    s3: Any,
    expires_seconds: int = 900,
) -> str | None:
    """Resolve a stored document locator, signing private S3 access on demand."""
    source = str(document_url or fallback_url or "").strip()
    if not source:
        return None
    if source.startswith("enc:"):
        from app.modules.runtime_shared_utils import decrypt

        source = decrypt(source, {"encryption_key": encryption_key})
    location = parse_object_url(source, endpoint_url)
    if not location or location[0] != str(private_bucket):
        return source
    return str(
        s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": location[0], "Key": location[1]},
            ExpiresIn=max(60, int(expires_seconds)),
        )
    )


def resolve_document_object_location(
    *,
    document_url: str | None,
    encryption_key: str,
    endpoint_url: str,
) -> tuple[str, str] | None:
    """Resolve a stored public or encrypted S3 locator into bucket and key."""
    source = str(document_url or "").strip()
    if not source:
        return None
    if source.startswith("enc:"):
        from app.modules.runtime_shared_utils import decrypt

        source = decrypt(source, {"encryption_key": encryption_key})
    return parse_object_url(source, endpoint_url)


__all__ = [
    "DEFAULT_MULTIPART_CHUNK_SIZE",
    "DocumentStorageSettings",
    "S3ConnectionSettings",
    "build_cache_index",
    "calculate_md5",
    "calculate_multipart_etag",
    "document_object_key",
    "find_valid_cache_file",
    "find_valid_cache_entry",
    "load_document_storage_settings",
    "normalized_extension",
    "object_url",
    "parse_object_url",
    "resolve_document_download_url",
    "resolve_document_object_location",
]
