"""Pure document cleanup classification and ISBN decision rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from app.modules.library.runtime.metadata.isbn_utils import canonicalize_isbn_values


TATAR_LANGUAGE_CODES = frozenset({"tat", "tt", "tatar"})
NON_DOCUMENT_MIME_TYPES = frozenset(
    {
        "application/javascript",
        "application/json",
        "application/octet-stream",
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
        "application/x-rar-compressed",
        "application/x-shockwave-flash",
        "application/x-tplink-bin",
        "application/x-zip-compressed",
        "application/zip",
        "text/css",
        "text/javascript",
        "text/x-algol68",
        "text/x-python",
        "text/x-python-script",
    }
)
NON_DOCUMENT_MIME_PREFIXES = ("audio/", "image/", "video/")


@dataclass(frozen=True)
class IsbnCleanupDecision:
    """One deterministic or human-reviewed duplicate ISBN decision."""

    isbn: str
    candidate_md5s: tuple[str, ...]
    keep_md5s: tuple[str, ...]
    remove_md5s: tuple[str, ...]
    requires_review: bool
    evidence: dict[str, Any]


def cleanup_reasons(*, language: Any, mime_type: Any, source_path: Any = "") -> list[str]:
    """Return independently observable reasons for moving a document aside."""
    reasons: list[str] = []
    normalized_language = str(language or "").strip().casefold()
    is_tatar = normalized_language in TATAR_LANGUAGE_CODES or normalized_language.startswith(
        ("tt-", "tat-")
    )
    if normalized_language and not is_tatar:
        reasons.append("non_tatar")
    normalized_mime = str(mime_type or "").split(";", 1)[0].strip().casefold()
    suffix = str(source_path or "").casefold()
    octet_pdf = normalized_mime == "application/octet-stream" and suffix.endswith(".pdf")
    if (not octet_pdf and normalized_mime in NON_DOCUMENT_MIME_TYPES) or normalized_mime.startswith(
        NON_DOCUMENT_MIME_PREFIXES
    ) or suffix.endswith((".eaf", ".musx")):
        reasons.append("non_document")
    return reasons


def build_isbn_cleanup_decisions(
    documents: Iterable[Mapping[str, Any]],
) -> list[IsbnCleanupDecision]:
    """Group duplicate ISBNs and auto-select only one clearly complete PDF."""
    grouped: dict[str, dict[str, Mapping[str, Any]]] = {}
    for document in documents:
        md5 = str(document.get("md5") or "").strip().lower()
        if len(md5) != 32:
            continue
        for isbn in canonicalize_isbn_values(document.get("isbn")) or []:
            grouped.setdefault(isbn, {})[md5] = document

    decisions: list[IsbnCleanupDecision] = []
    for isbn, candidates_by_md5 in sorted(grouped.items()):
        if len(candidates_by_md5) < 2:
            continue
        candidates = tuple(sorted(candidates_by_md5))
        complete_pdfs = tuple(
            md5
            for md5 in candidates
            if bool(candidates_by_md5[md5].get("full"))
            and str(candidates_by_md5[md5].get("mime_type") or "").casefold()
            == "application/pdf"
        )
        deterministic = len(complete_pdfs) == 1
        keep = complete_pdfs if deterministic else ()
        remove = tuple(md5 for md5 in candidates if md5 not in keep) if deterministic else ()
        decisions.append(
            IsbnCleanupDecision(
                isbn=isbn,
                candidate_md5s=candidates,
                keep_md5s=keep,
                remove_md5s=remove,
                requires_review=not deterministic,
                evidence={
                    "candidate_count": len(candidates),
                    "complete_pdf_count": len(complete_pdfs),
                    "rule": "single_complete_pdf" if deterministic else "human_review",
                },
            )
        )
    return decisions


__all__ = [
    "IsbnCleanupDecision",
    "NON_DOCUMENT_MIME_TYPES",
    "TATAR_LANGUAGE_CODES",
    "build_isbn_cleanup_decisions",
    "cleanup_reasons",
]
