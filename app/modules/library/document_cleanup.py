"""Pure document cleanup classification and ISBN decision rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from app.document_sync_filter import classify_document
from app.modules.library.runtime.metadata.isbn_utils import canonicalize_isbn_values


TATAR_LANGUAGE_CODES = frozenset({"tat", "tt", "tatar"})
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
    if not classify_document(str(source_path or ""), str(mime_type or "")).accepted:
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
    "TATAR_LANGUAGE_CODES",
    "build_isbn_cleanup_decisions",
    "cleanup_reasons",
]
