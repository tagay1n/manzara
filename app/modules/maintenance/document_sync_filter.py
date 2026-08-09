"""Shared eligibility policy for document synchronization tasks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


NON_DOCUMENT_MIME_TYPES = frozenset(
    {
        "application/javascript",
        "application/json",
        "application/octet",
        "application/vnd.android.package-archive",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/x-7z-compressed",
        "application/x-chm",
        "application/x-download",
        "application/x-gzip",
        "application/x-javascript",
        "application/x-rar",
        "application/x-shockwave-flash",
        "application/x-tplink-bin",
        "application/x-zip-compressed",
        "application/zip",
        "audio/midi",
        "audio/mp3",
        "audio/mpeg",
        "audio/x-wav",
        "image/bmp",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/vnd.adobe.photoshop",
        "image/x-icon",
        "text/css",
        "text/javascript",
        "text/x-algol68",
        "text/x-python",
        "text/x-python-script",
        "video/3gpp",
        "video/mp4",
        "video/x-unknown",
    }
)


@dataclass(frozen=True)
class DocumentFilterDecision:
    accepted: bool
    mime_type: str
    reason: str | None = None


def normalize_document_mime(source_path: str, mime_type: str) -> str:
    normalized = str(mime_type or "").strip().casefold()
    suffix = PurePosixPath(str(source_path)).suffix.casefold()
    if normalized == "application/octet-stream" and suffix == ".pdf":
        return "application/pdf"
    if normalized == "text/html" and suffix in {".txt", ".doc"}:
        return "text/plain"
    return normalized or "application/octet-stream"


def classify_document(source_path: str, mime_type: str) -> DocumentFilterDecision:
    """Apply the original monocorpus sync exclusions without mutating storage."""
    normalized_path = str(source_path or "").removeprefix("disk:").casefold()
    suffix = PurePosixPath(normalized_path).suffix
    normalized_mime = normalize_document_mime(source_path, mime_type)

    if (
        normalized_path.startswith(
            "/neurotatarlar/kitaplar/monocorpus/anna's archive/"
        )
        and suffix == ".txt"
    ):
        return DocumentFilterDecision(False, normalized_mime, "annas_archive_text")
    if (
        "/random_files_thru_yandex_search/ilbyak-school.narod.ru/"
        in normalized_path
        and suffix in {".htm", ".html"}
    ):
        return DocumentFilterDecision(False, normalized_mime, "ilbyak_html")
    if suffix in {".eaf", ".musx"}:
        return DocumentFilterDecision(False, normalized_mime, "non_document_suffix")
    if normalized_mime in NON_DOCUMENT_MIME_TYPES:
        return DocumentFilterDecision(False, normalized_mime, "non_document_mime")
    return DocumentFilterDecision(True, normalized_mime)


__all__ = [
    "DocumentFilterDecision",
    "NON_DOCUMENT_MIME_TYPES",
    "classify_document",
    "normalize_document_mime",
]
