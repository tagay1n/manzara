"""Document cleanup planning service with no remote mutation capability."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping

from app.modules.library.document_cleanup import (
    build_isbn_cleanup_decisions,
    cleanup_reasons,
)
from app.modules.library.runtime.metadata.fields import extract_isbn_values, parse_meta


def cleanup_target_path(
    filtered_out_path: str,
    *,
    reason: str,
    md5: str,
    source_path: str,
) -> str:
    """Build a collision-resistant but recognizable filtered-out path."""
    source_name = PurePosixPath(str(source_path).removeprefix("disk:")).name
    safe_name = source_name or "document"
    root = str(filtered_out_path).removeprefix("disk:").rstrip("/")
    return f"{root}/{reason}/{md5}_{safe_name}"


def prepare_document_cleanup(
    *,
    repository: Any,
    filtered_out_path: str,
    should_stop: Callable[[], bool] = lambda: False,
    on_progress: Callable[[int, int, Mapping[str, int]], None] | None = None,
) -> dict[str, Any]:
    """Create safe cleanup plans and reviews without touching storage or documents."""
    documents = repository.list_documents_for_planning()
    counters = {
        "scanned": 0,
        "plans_created": 0,
        "plans_reused": 0,
        "isbn_groups": 0,
        "isbn_reviews_created": 0,
        "isbn_reviews_reused": 0,
    }
    isbn_documents: list[dict[str, Any]] = []
    by_md5: dict[str, dict[str, Any]] = {}
    total = len(documents)
    for document in documents:
        if should_stop():
            break
        md5 = str(document.get("md5") or "").strip().lower()
        source_path = str(document.get("ya_path") or "").strip()
        if not md5 or not source_path:
            continue
        by_md5[md5] = dict(document)
        reasons = cleanup_reasons(
            language=document.get("language"),
            mime_type=document.get("mime_type"),
            source_path=source_path,
        )
        if reasons:
            reason = reasons[0]
            _, created = repository.enqueue_cleanup(
                {
                    "scope": "document",
                    "action": "move",
                    "reason": reason,
                    "md5": md5,
                    "source_resource_id": document.get("ya_resource_id") or None,
                    "source_path": source_path,
                    "target_path": cleanup_target_path(
                        filtered_out_path,
                        reason=reason,
                        md5=md5,
                        source_path=source_path,
                    ),
                    "evidence": {"reasons": reasons},
                }
            )
            counters["plans_created" if created else "plans_reused"] += 1
        schema_org = parse_meta(document.get("schema_org"))
        isbn_values = extract_isbn_values(schema_org)
        if isbn_values:
            isbn_documents.append(
                {
                    "md5": md5,
                    "isbn": isbn_values,
                    "full": document.get("full"),
                    "mime_type": document.get("mime_type"),
                    "source_path": source_path,
                    "title": str(schema_org.get("name") or ""),
                }
            )
        counters["scanned"] += 1
        if on_progress and (
            counters["scanned"] == total or counters["scanned"] % 1000 == 0
        ):
            on_progress(counters["scanned"], total, counters)

    for decision in build_isbn_cleanup_decisions(isbn_documents):
        if should_stop():
            break
        counters["isbn_groups"] += 1
        candidates = [
            {
                "md5": md5,
                "title": next(
                    (
                        str(item.get("title") or "")
                        for item in isbn_documents
                        if item["md5"] == md5
                    ),
                    "",
                ),
                "source_path": str(by_md5[md5].get("ya_path") or ""),
                "mime_type": str(by_md5[md5].get("mime_type") or ""),
                "full": bool(by_md5[md5].get("full")),
            }
            for md5 in decision.candidate_md5s
        ]
        if decision.requires_review:
            _, created = repository.upsert_isbn_review(
                isbn=decision.isbn,
                candidates=candidates,
                evidence=decision.evidence,
            )
            counters[
                "isbn_reviews_created" if created else "isbn_reviews_reused"
            ] += 1
            continue
        for md5 in decision.remove_md5s:
            document = by_md5[md5]
            source_path = str(document.get("ya_path") or "")
            _, created = repository.enqueue_cleanup(
                {
                    "scope": "document",
                    "action": "move",
                    "reason": "duplicate_isbn",
                    "md5": md5,
                    "source_resource_id": document.get("ya_resource_id") or None,
                    "source_path": source_path,
                    "target_path": cleanup_target_path(
                        filtered_out_path,
                        reason="duplicate_isbn",
                        md5=md5,
                        source_path=source_path,
                    ),
                    "evidence": {
                        "isbn": decision.isbn,
                        "keep_md5s": list(decision.keep_md5s),
                        **decision.evidence,
                    },
                }
            )
            counters["plans_created" if created else "plans_reused"] += 1
    summary = {
        "kind": "library.document_cleanup_preparation_summary",
        **counters,
        "stopped": bool(should_stop()),
    }
    print(
        f"document cleanup preparation: final {json.dumps(summary, sort_keys=True)}",
        flush=True,
    )
    return summary


def apply_isbn_review_decision(
    *,
    repository: Any,
    review_id: int,
    keep_md5s: list[str],
    filtered_out_path: str,
) -> dict[str, Any]:
    """Persist a review decision and queue every non-kept document."""
    decision = repository.decide_review(review_id, keep_md5s=keep_md5s)
    queued = 0
    for candidate in decision["remove_candidates"]:
        md5 = str(candidate["md5"])
        source_path = str(candidate.get("source_path") or "")
        _, created = repository.enqueue_cleanup(
            {
                "scope": "document",
                "action": "move",
                "reason": "duplicate_isbn",
                "md5": md5,
                "source_resource_id": None,
                "source_path": source_path,
                "target_path": cleanup_target_path(
                    filtered_out_path,
                    reason="duplicate_isbn",
                    md5=md5,
                    source_path=source_path,
                ),
                "evidence": {
                    "isbn": decision["isbn"],
                    "review_id": review_id,
                    "keep_md5s": decision["keep_md5s"],
                },
            }
        )
        queued += int(created)
    return {**decision, "queued": queued}


__all__ = [
    "apply_isbn_review_decision",
    "cleanup_target_path",
    "prepare_document_cleanup",
]
