"""Shared configuration and integrity helpers for primary document storage."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, unquote, urlparse


DEFAULT_S3_ENDPOINT = "https://storage.yandexcloud.net"
DEFAULT_S3_REGION = "ru-central1"
DEFAULT_MULTIPART_CHUNK_SIZE = 8 * 1024 * 1024
DEFAULT_DOCUMENT_CACHE_MAX_BYTES = 50 * 1024**3
DOCUMENT_CACHE_TARGET_NUMERATOR = 9
DOCUMENT_CACHE_TARGET_DENOMINATOR = 10
ABANDONED_CACHE_DOWNLOAD_SECONDS = 24 * 60 * 60
_PARTIAL_SUFFIXES = {".crdownload", ".download", ".part", ".partial", ".tmp"}
_SAFE_EXTENSION = re.compile(r"^\.[a-z0-9]{1,12}$")
_CACHE_ENTRY_NAME = re.compile(r"^[a-f0-9]{32}\.[a-z0-9]{1,12}$")
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
    filtered_out_path: str
    primary: S3ConnectionSettings
    legacy: S3ConnectionSettings
    public_bucket: str
    private_bucket: str
    legacy_public_bucket: str
    legacy_private_bucket: str
    encryption_key: str
    cache_max_bytes: int = DEFAULT_DOCUMENT_CACHE_MAX_BYTES
    yadisk_token: str = ""
    preview_bucket: str = ""
    content_bucket: str = ""
    content_images_bucket: str = ""


@dataclass(frozen=True)
class CachePruneResult:
    """Outcome of one best-effort shared-cache size enforcement pass."""

    initial_bytes: int
    remaining_bytes: int
    removed_files: int
    removed_bytes: int
    failed_paths: tuple[Path, ...] = ()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _required(mapping: Mapping[str, Any], key: str, path: str) -> str:
    value = str(mapping.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required config value: {path}.{key}")
    return value


def _cache_max_bytes(documents: Mapping[str, Any]) -> int:
    value = documents.get("cache_max_gib", 50)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError("documents.cache_max_gib must be a positive integer")
    return value * 1024**3


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
        filtered_out_path=_required(
            disk_documents, "filtered_out_path", "yandex.disk.documents"
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
        encryption_key=_required(payload, "encryption_key", "config"),
        cache_max_bytes=_cache_max_bytes(documents),
        yadisk_token=_required(disk, "oauth_token", "yandex.disk"),
        preview_bucket=str(primary_buckets.get("book_previews") or "").strip(),
        content_bucket=str(primary_buckets.get("content") or "").strip(),
        content_images_bucket=str(
            primary_buckets.get("content_images") or ""
        ).strip(),
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


def prune_document_cache(
    cache_path: Path,
    *,
    max_bytes: int = DEFAULT_DOCUMENT_CACHE_MAX_BYTES,
    target_bytes: int | None = None,
    protected_paths: Iterable[Path] = (),
) -> CachePruneResult:
    """Evict oldest completed entries when the shared cache exceeds its limit."""
    maximum = int(max_bytes)
    if maximum <= 0:
        raise ValueError("max_bytes must be positive")
    target = (
        int(target_bytes)
        if target_bytes is not None
        else maximum
        * DOCUMENT_CACHE_TARGET_NUMERATOR
        // DOCUMENT_CACHE_TARGET_DENOMINATOR
    )
    if target < 0 or target > maximum:
        raise ValueError("target_bytes must be between zero and max_bytes")

    root = Path(cache_path)
    if not root.is_dir():
        return CachePruneResult(0, 0, 0, 0)
    protected = {Path(path).resolve(strict=False) for path in protected_paths}
    total = 0
    candidates: list[tuple[int, str, int, Path]] = []
    abandoned_before_ns = time.time_ns() - ABANDONED_CACHE_DOWNLOAD_SECONDS * 10**9
    try:
        entries = os.scandir(root)
    except OSError:
        return CachePruneResult(0, 0, 0, 0, (root,))
    with entries:
        for entry in entries:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            size = int(stat.st_size)
            total += size
            path = Path(entry.path)
            is_completed = _CACHE_ENTRY_NAME.fullmatch(entry.name.lower()) is not None
            is_abandoned_download = (
                path.suffix.lower() in _PARTIAL_SUFFIXES
                and int(stat.st_mtime_ns) < abandoned_before_ns
            )
            if (is_completed or is_abandoned_download) and (
                path.resolve(strict=False) not in protected
            ):
                candidates.append((int(stat.st_mtime_ns), entry.name, size, path))

    initial = total
    if total <= maximum:
        return CachePruneResult(initial, total, 0, 0)

    removed_files = 0
    removed_bytes = 0
    failed: list[Path] = []
    for _mtime, _name, size, path in sorted(candidates):
        if total <= target:
            break
        try:
            path.unlink()
        except FileNotFoundError:
            total -= size
        except OSError:
            failed.append(path)
            continue
        else:
            total -= size
            removed_files += 1
            removed_bytes += size

    print(
        "document cache: pruned "
        f"initial_bytes={initial} remaining_bytes={max(0, total)} "
        f"removed_files={removed_files} removed_bytes={removed_bytes} "
        f"failed_files={len(failed)}",
        flush=True,
    )
    return CachePruneResult(
        initial,
        max(0, total),
        removed_files,
        removed_bytes,
        tuple(failed),
    )


def find_valid_cache_entry(
    cache_index: Mapping[str, Iterable[Path]],
    md5: str,
) -> tuple[Path, str] | None:
    """Return a matching cache path and its multipart ETag."""
    digest = str(md5 or "").strip().lower()
    for candidate in cache_index.get(digest, []):
        try:
            actual_md5, multipart_etag = calculate_integrity(candidate)
        except FileNotFoundError:
            continue
        if actual_md5 == digest:
            try:
                candidate.touch()
            except OSError:
                pass
            return candidate, multipart_etag
    return None


def find_valid_cache_file(cache_path: Path, md5: str) -> Path | None:
    """Find a non-partial cache file whose bytes match the requested MD5."""
    digest = str(md5 or "").strip().lower()
    entry = find_valid_cache_entry(build_cache_index(cache_path), digest)
    return entry[0] if entry else None


def materialize_cached_document(
    *,
    cache_path: Path,
    expected_md5: str,
    extension: str,
    download: Callable[[Path], None],
    cache_max_bytes: int = DEFAULT_DOCUMENT_CACHE_MAX_BYTES,
) -> Path:
    """Reuse or atomically populate one MD5-verified shared cache file."""
    digest = str(expected_md5 or "").strip().lower()
    if len(digest) != 32 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"Invalid document MD5: {expected_md5!r}")
    suffix = str(extension or "").strip().lower()
    if suffix and not suffix.startswith("."):
        suffix = "." + suffix
    if not _SAFE_EXTENSION.fullmatch(suffix):
        suffix = ".bin"

    cache_path = Path(cache_path)
    cache_path.mkdir(parents=True, exist_ok=True)
    destination = cache_path / f"{digest}{suffix}"
    if destination.is_file() and calculate_md5(destination) == digest:
        try:
            destination.touch()
        except OSError:
            pass
        return destination
    if cached := find_valid_cache_file(cache_path, digest):
        return cached

    temporary_handle = tempfile.NamedTemporaryFile(
        dir=cache_path,
        prefix=f".{digest}.",
        suffix=".download",
        delete=False,
    )
    temporary = Path(temporary_handle.name)
    temporary_handle.close()
    try:
        download(temporary)
        actual_md5 = calculate_md5(temporary)
        if actual_md5 != digest:
            raise ValueError(
                f"Downloaded document MD5 mismatch: expected={digest} actual={actual_md5}"
            )
        if cached := find_valid_cache_file(cache_path, digest):
            return cached
        temporary.replace(destination)
        prune_document_cache(
            cache_path,
            max_bytes=cache_max_bytes,
            protected_paths=(destination,),
        )
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def download_cached_primary_document(
    *,
    settings: DocumentStorageSettings,
    s3: Any,
    document_url: str,
    expected_md5: str,
    expected_size: int | None = None,
    extension: str,
) -> Path:
    """Reuse shared cache or download a verified primary-storage document into it."""

    def download(destination: Path) -> None:
        location = verify_primary_document_object(
            settings=settings,
            s3=s3,
            document_url=document_url,
            expected_size=expected_size,
        )
        s3.download_file(location[0], location[1], str(destination))

    return materialize_cached_document(
        cache_path=settings.cache_path,
        expected_md5=expected_md5,
        extension=extension,
        download=download,
        cache_max_bytes=settings.cache_max_bytes,
    )


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


def download_verified_primary_document(
    *,
    settings: DocumentStorageSettings,
    s3: Any,
    document_url: str,
    expected_md5: str,
    expected_size: int | None = None,
    destination: Path,
) -> Path:
    """Download one document exclusively from configured primary storage."""
    location = verify_primary_document_object(
        settings=settings,
        s3=s3,
        document_url=document_url,
        expected_size=expected_size,
    )

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".download")
    temporary.unlink(missing_ok=True)
    try:
        s3.download_file(location[0], location[1], str(temporary))
        actual_md5 = calculate_md5(temporary)
        if actual_md5 != str(expected_md5 or "").strip().lower():
            raise ValueError(
                f"Primary document MD5 mismatch: expected={expected_md5} actual={actual_md5}"
            )
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def verify_primary_document_object(
    *,
    settings: DocumentStorageSettings,
    s3: Any,
    document_url: str,
    expected_size: int | None,
) -> tuple[str, str]:
    """Require an existing object in one configured primary-storage bucket."""
    location = resolve_document_object_location(
        document_url=document_url,
        encryption_key=settings.encryption_key,
        endpoint_url=settings.primary.endpoint_url,
    )
    if location is None or location[0] not in {
        settings.public_bucket,
        settings.private_bucket,
    }:
        raise ValueError("Document URL is not in configured primary Backblaze storage")
    response = s3.head_object(Bucket=location[0], Key=location[1])
    remote_size = int(response.get("ContentLength") or 0)
    if expected_size is not None and remote_size != int(expected_size):
        raise ValueError(
            f"Primary document size mismatch: expected={expected_size} actual={remote_size}"
        )
    return location


__all__ = [
    "CachePruneResult",
    "DEFAULT_DOCUMENT_CACHE_MAX_BYTES",
    "DEFAULT_MULTIPART_CHUNK_SIZE",
    "DocumentStorageSettings",
    "S3ConnectionSettings",
    "build_cache_index",
    "calculate_md5",
    "calculate_multipart_etag",
    "document_object_key",
    "download_cached_primary_document",
    "download_verified_primary_document",
    "find_valid_cache_file",
    "find_valid_cache_entry",
    "load_document_storage_settings",
    "materialize_cached_document",
    "normalized_extension",
    "object_url",
    "prune_document_cache",
    "parse_object_url",
    "resolve_document_download_url",
    "resolve_document_object_location",
    "verify_primary_document_object",
]
