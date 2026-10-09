"""Document cleanup planning service with no remote mutation capability."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from app.document_cleanup_paths import cleanup_target_path
from app.document_operation_lock import DocumentOperationBusy
from app.modules.library.document_cleanup import (
    build_isbn_cleanup_decisions,
    cleanup_reasons,
)


def prepare_document_cleanup(
    *,
    repository: Any,
    filtered_out_path: str,
    source_root_path: str,
    should_stop: Callable[[], bool] = lambda: False,
    log: Callable[[str], None] = print,
    on_progress: Callable[[int, int, Mapping[str, int]], None] | None = None,
) -> dict[str, Any]:
    """Create safe cleanup plans and reviews without touching storage or documents."""
    reconciliation = repository.reconcile_pending_reviews()
    documents = repository.list_documents_for_planning()
    counters = {
        "scanned": 0,
        "skipped_missing_source": 0,
        "plans_created": 0,
        "plans_reused": 0,
        "plans_suppressed": 0,
        "plans_deferred": 0,
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
        counters["scanned"] += 1
        md5 = str(document.get("md5") or "").strip().lower()
        source_path = str(document.get("ya_path") or "").strip()
        if not md5 or not source_path:
            counters["skipped_missing_source"] += 1
            log(f"cleanup skipped md5={md5} reason=missing_source_path")
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
                "evidence": {"reasons": reasons, **{key: document.get(key) for key in
                    ("publication_id", "document_revision", "publication_revision", "source_revision")}},
            }
            if repository.is_cleanup_suppressed(payload):
                counters["plans_suppressed"] += 1
            else:
                try:
                    cleanup_id, created = repository.enqueue_cleanup(payload)
                except DocumentOperationBusy as exc:
                    counters['plans_deferred'] += 1
                    log(f'cleanup plan deferred md5={md5} error={exc}')
                    continue
                log(f"cleanup plan md5={md5} cleanup_id={cleanup_id} reason={reason} created={created}")
                counters["plans_created" if created else "plans_reused"] += 1
                planned_by_reason[reason] += 1
                counters[f"planned_{reason}"] += 1
        isbn_values = document.get("isbn") or []
        if isbn_values and not reasons:
            isbn_documents.append(
                {
                    "md5": md5,
                    "isbn": isbn_values,
                    "full": document.get("full"),
                    "mime_type": document.get("mime_type"),
                    "source_path": source_path,
                    "title": str(document.get("title") or ""),
                }
            )
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
                "page_count": by_md5[md5].get("page_count"),
                "source_resource_id": by_md5[md5].get("ya_resource_id"),
                **{key: by_md5[md5].get(key) for key in
                   ("publication_id", "document_revision", "publication_revision", "source_revision")},
            }
            for md5 in decision.candidate_md5s
        ]
        counters["isbn_review_groups"] += 1
        counters["isbn_review_candidates"] += len(decision.candidate_md5s)
        review_id, created = repository.upsert_isbn_review(
            isbn=decision.isbn,
            candidates=candidates,
            evidence={
                **decision.evidence,
                "recommended_keep_md5s": list(decision.keep_md5s),
                "automatic_cleanup": False,
            },
        )
        log(f"cleanup ISBN review review_id={review_id} candidates={len(candidates)} created={created}")
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
        "review_reconciliation": dict(reconciliation),
        "stopped": bool(should_stop()),
    }
    if on_progress:
        on_progress(counters["scanned"], total, counters)
    log(f"document cleanup preparation: final {json.dumps(summary, sort_keys=True)}")
    return summary


def apply_isbn_review_decision(
    *,
    repository: Any,
    review_id: int,
    keep_md5s: list[str],
    expected_snapshot: str,
    filtered_out_path: str,
    source_root_path: str,
) -> dict[str, Any]:
    """Persist a review decision and queue every non-kept document."""
    with repository.write_transaction() as conn:
        return _queue_review_decision(repository, conn, review_id, keep_md5s, expected_snapshot, filtered_out_path, source_root_path)


def _queue_review_decision(repository, conn, review_id, keep_md5s, expected_snapshot, filtered_out_path, source_root_path):
    decision = repository.decide_review(review_id, keep_md5s=keep_md5s, expected_snapshot=expected_snapshot, conn=conn)
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
                "source_resource_id": candidate.get("source_resource_id"),
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
                    **{key: candidate.get(key) for key in
                       ("publication_id", "document_revision", "publication_revision", "source_revision")},
                },
            }, conn=conn,
        )
        queued += int(created)
    return {**decision, "queued": queued}


__all__ = [
    "apply_isbn_review_decision",
    "cleanup_target_path",
    "prepare_document_cleanup",
]
