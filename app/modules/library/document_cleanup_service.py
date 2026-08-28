"""Document cleanup planning service with no remote mutation capability."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from app.document_cleanup_paths import cleanup_target_path
from app.modules.library.document_cleanup import (
    build_isbn_cleanup_decisions,
    cleanup_reasons,
)
from app.modules.library.runtime.metadata.fields import extract_isbn_values, parse_meta


def prepare_document_cleanup(
    *,
    repository: Any,
    filtered_out_path: str,
    source_root_path: str,
    should_stop: Callable[[], bool] = lambda: False,
    on_progress: Callable[[int, int, Mapping[str, int]], None] | None = None,
) -> dict[str, Any]:
    """Create safe cleanup plans and reviews without touching storage or documents."""
    documents = repository.list_documents_for_planning()
    counters = {
        "scanned": 0,
        "plans_created": 0,
        "plans_reused": 0,
        "plans_suppressed": 0,
        "planned_duplicate_isbn": 0,
        "planned_non_document": 0,
        "planned_non_tatar": 0,
        "isbn_groups": 0,
        "isbn_auto_resolved_groups": 0,
        "isbn_review_groups": 0,
        "isbn_review_candidates": 0,
        "isbn_reviews_created": 0,
        "isbn_reviews_reused": 0,
    }
    planned_by_reason = {
        "duplicate_isbn": 0,
        "non_document": 0,
        "non_tatar": 0,
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
            payload = {
                "scope": "document",
                "action": "move",
                "reason": reason,
                "md5": md5,
                "source_resource_id": document.get("ya_resource_id") or None,
                "source_path": source_path,
                "target_path": cleanup_target_path(
                    filtered_out_path,
                    reason=reason,
                    source_root_path=source_root_path,
                    source_path=source_path,
                ),
                "evidence": {"reasons": reasons},
            }
            if repository.is_cleanup_suppressed(payload):
                counters["plans_suppressed"] += 1
            else:
                _, created = repository.enqueue_cleanup(payload)
                counters["plans_created" if created else "plans_reused"] += 1
                planned_by_reason[reason] += 1
                counters[f"planned_{reason}"] += 1
        schema_org = parse_meta(document.get("schema_org"))
        isbn_values = extract_isbn_values(schema_org)
        if isbn_values and not reasons:
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
        counters["isbn_review_groups"] += 1
        counters["isbn_review_candidates"] += len(decision.candidate_md5s)
        _, created = repository.upsert_isbn_review(
            isbn=decision.isbn,
            candidates=candidates,
            evidence={
                **decision.evidence,
                "recommended_keep_md5s": list(decision.keep_md5s),
                "automatic_cleanup": False,
            },
        )
        counters[
            "isbn_reviews_created" if created else "isbn_reviews_reused"
        ] += 1
    summary = {
        "kind": "library.document_cleanup_preparation_summary",
        **counters,
        "planned_by_reason": planned_by_reason,
        "planned_moves": {
            "total": sum(planned_by_reason.values()),
            "by_isbn": planned_by_reason["duplicate_isbn"],
            "by_language": planned_by_reason["non_tatar"],
            "by_non_document_format": planned_by_reason["non_document"],
        },
        "isbn_analysis": {
            "duplicate_groups": counters["isbn_groups"],
            "auto_resolved_groups": counters["isbn_auto_resolved_groups"],
            "books_planned_to_move": planned_by_reason["duplicate_isbn"],
            "review_groups": counters["isbn_review_groups"],
            "books_awaiting_review": counters["isbn_review_candidates"],
        },
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
    source_root_path: str,
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
                    source_root_path=source_root_path,
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
