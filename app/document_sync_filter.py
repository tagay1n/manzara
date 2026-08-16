"""Shared eligibility policy for document synchronization tasks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


NON_DOCUMENT_MIME_TYPES = frozenset(
    {
        "application/javascript",
        "application/json",
        "application/octet",
        "application/rar",
        "application/vnd.android.package-archive",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.rar",
        "application/x-7z-compressed",
        "application/x-bittorrent",
        "application/x-chm",
        "application/x-download",
        "application/x-gzip",
        "application/x-javascript",
        "application/x-ms-shortcut",
        "application/x-rar",
        "application/x-rar-compressed",
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

DOCUMENT_MIME_BY_SUFFIX = {
    ".djv": "image/vnd.djvu",
    ".djvu": "image/vnd.djvu",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".epub": "application/epub+zip",
    ".fb2": "application/x-fictionbook+xml",
    ".htm": "text/html",
    ".html": "text/html",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".pdf": "application/pdf",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".rtf": "application/rtf",
    ".txt": "text/plain",
}
DOCUMENT_MIME_TYPES = frozenset(DOCUMENT_MIME_BY_SUFFIX.values())
NON_DOCUMENT_SUFFIXES = frozenset({".eaf", ".lnk", ".musx"})
NON_DOCUMENT_MIME_PREFIXES = ("audio/", "image/", "video/")


@dataclass(frozen=True)
class DocumentFilterDecision:
    accepted: bool
    mime_type: str
    reason: str | None = None


def normalize_document_mime(source_path: str, mime_type: str) -> str:
    normalized = str(mime_type or "").split(";", 1)[0].strip().casefold()
    suffix = PurePosixPath(str(source_path)).suffix.casefold()
    if normalized in {"", "application/octet-stream"} and suffix in DOCUMENT_MIME_BY_SUFFIX:
        return DOCUMENT_MIME_BY_SUFFIX[suffix]
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
    if suffix in NON_DOCUMENT_SUFFIXES:
        return DocumentFilterDecision(False, normalized_mime, "non_document_suffix")
    if normalized_mime in DOCUMENT_MIME_TYPES:
        return DocumentFilterDecision(True, normalized_mime)
    if normalized_mime in NON_DOCUMENT_MIME_TYPES or normalized_mime.startswith(
        NON_DOCUMENT_MIME_PREFIXES
    ):
        return DocumentFilterDecision(False, normalized_mime, "non_document_mime")
    if normalized_mime == "application/octet-stream":
        return DocumentFilterDecision(False, normalized_mime, "non_document_unknown_binary")
    return DocumentFilterDecision(True, normalized_mime)


__all__ = [
    "DocumentFilterDecision",
    "DOCUMENT_MIME_BY_SUFFIX",
    "DOCUMENT_MIME_TYPES",
    "NON_DOCUMENT_MIME_TYPES",
    "NON_DOCUMENT_SUFFIXES",
    "classify_document",
    "normalize_document_mime",
]
