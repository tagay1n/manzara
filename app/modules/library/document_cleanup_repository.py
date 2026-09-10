"""PostgreSQL persistence for document cleanup plans and ISBN reviews."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import text

from app.modules.library.runtime.metadata.fields import extract_page_count, parse_meta
from app.postgres_engine import acquire_postgres_engine, release_postgres_engine


def _enrich_review_page_counts(
    reviews: Iterable[Mapping[str, Any]],
    page_counts: Mapping[str, int | None],
) -> list[dict[str, Any]]:
    """Attach current metadata page counts without changing persisted review identity."""
    enriched_reviews: list[dict[str, Any]] = []
    for review in reviews:
        enriched = dict(review)
        enriched["candidates_json"] = [
            {
                **dict(candidate),
                "page_count": page_counts.get(
                    str(candidate.get("md5") or "").strip().lower()
                ),
            }
            for candidate in review.get("candidates_json") or []
        ]
        enriched_reviews.append(enriched)
    return enriched_reviews


def _removed_review_md5s(review: Mapping[str, Any]) -> set[str]:
    kept = {
        str(value).strip().lower()
        for value in review.get("keep_md5s_json") or []
    }
    return {
        str(candidate.get("md5") or "").strip().lower()
        for candidate in review.get("candidates_json") or []
        if candidate.get("md5")
    } - kept


def _shared_removed_md5s(reviews: Iterable[Mapping[str, Any]]) -> set[str]:
    shared: set[str] = set()
    for review in reviews:
        shared.update(_removed_review_md5s(review))
    return shared


class DocumentCleanupRepository:
    """Own cleanup planning, review, claiming, and status transitions."""

    def __init__(self, database_url: str, *, schema: str) -> None:
        self.engine = acquire_postgres_engine(database_url, schema=schema)

    def dispose(self) -> None:
        release_postgres_engine(self.engine)

    def list_documents_for_planning(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT d.md5, d.mime_type, d.ya_path, d.ya_resource_id,
                               d.language, d."full", d.sharing_restricted,
                               m.schema_org
                        FROM document d
                        LEFT JOIN metadata m ON m.md5 = d.md5
                        WHERE d.md5 IS NOT NULL
                        ORDER BY d.md5
                        """
                    )
                ).mappings()
            ]

    def enqueue_cleanup(self, payload: Mapping[str, Any]) -> tuple[int, bool]:
        values = {
            **dict(payload),
            "evidence_json": json.dumps(
                payload.get("evidence") or {}, ensure_ascii=False, sort_keys=True
            ),
        }
        with self.engine.begin() as conn:
            if str(values["scope"]) in {"duplicate_resource", "source_resource"}:
                existing = conn.execute(
                    text(
                        """
                        SELECT cleanup_id FROM document_cleanup_queue
                        WHERE scope = :scope
                          AND source_path = :source_path
                          AND status IN ('planned', 'running', 'failed')
                        """
                    ),
                    values,
                ).scalar_one_or_none()
            else:
                existing = conn.execute(
                    text(
                        """
                        SELECT cleanup_id FROM document_cleanup_queue
                        WHERE scope = 'document' AND md5 = :md5
                          AND status IN ('planned', 'running', 'failed')
                        """
                    ),
                    values,
                ).scalar_one_or_none()
            if existing is not None:
                return int(existing), False
            cleanup_id = conn.execute(
                text(
                    """
                    INSERT INTO document_cleanup_queue (
                        scope, action, reason, md5, source_resource_id,
                        source_path, target_path, evidence_json
                    ) VALUES (
                        :scope, :action, :reason, :md5, :source_resource_id,
                        :source_path, :target_path, CAST(:evidence_json AS JSONB)
                    ) RETURNING cleanup_id
                    """
                ),
                values,
            ).scalar_one()
        return int(cleanup_id), True

    def is_cleanup_suppressed(self, payload: Mapping[str, Any]) -> bool:
        """Return whether the same missing/unsafe plan was explicitly canceled."""
        with self.engine.connect() as conn:
            return bool(
                conn.execute(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM document_cleanup_queue
                            WHERE scope=:scope AND md5=:md5 AND reason=:reason
                              AND source_path=:source_path AND status='canceled'
                        )
                        """
                    ),
                    {
                        "scope": str(payload.get("scope") or ""),
                        "md5": str(payload.get("md5") or ""),
                        "reason": str(payload.get("reason") or ""),
                        "source_path": str(payload.get("source_path") or ""),
                    },
                ).scalar_one()
            )

    def upsert_isbn_review(
        self,
        *,
        isbn: str,
        candidates: Iterable[Mapping[str, Any]],
        evidence: Mapping[str, Any],
    ) -> tuple[int, bool]:
        candidate_list = sorted(
            (dict(candidate) for candidate in candidates),
            key=lambda item: str(item.get("md5") or ""),
        )
        canonical = json.dumps(candidate_list, ensure_ascii=False, sort_keys=True)
        candidate_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        values = {
            "isbn": isbn,
            "candidate_hash": candidate_hash,
            "candidates_json": canonical,
            "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        }
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO library_isbn_duplicate_reviews (
                        isbn, candidate_hash, candidates_json, evidence_json
                    ) VALUES (
                        :isbn, :candidate_hash, CAST(:candidates_json AS JSONB),
                        CAST(:evidence_json AS JSONB)
                    )
                    ON CONFLICT (isbn, candidate_hash) DO UPDATE SET
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING review_id, (xmax = 0) AS inserted
                    """
                ),
                values,
            ).mappings().one()
        return int(row["review_id"]), bool(row["inserted"])

    def list_queue(self, *, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        where = "WHERE status = :status" if status else ""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT cleanup_id, scope, action, reason, md5,
                           source_resource_id, source_path, target_path, status,
                           phase, evidence_json, attempts, run_id, last_error,
                           created_at, updated_at, completed_at
                    FROM document_cleanup_queue
                    {where}
                    ORDER BY cleanup_id DESC LIMIT :limit
                    """
                ),
                {"status": status, "limit": max(1, min(int(limit), 500))},
            ).mappings()
            return [dict(row) for row in rows]

    def get_overview(self) -> dict[str, int]:
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE status IN ('planned', 'running', 'failed')) AS active_plans,
                        COUNT(*) FILTER (WHERE status = 'failed') AS failed_plans,
                        COUNT(*) FILTER (WHERE status = 'completed') AS completed_plans
                    FROM document_cleanup_queue
                    """
                )
            ).mappings().one()
            pending_reviews = conn.execute(
                text(
                    "SELECT COUNT(*) FROM library_isbn_duplicate_reviews WHERE status='pending'"
                )
            ).scalar_one()
        return {
            "active_plans": int(row["active_plans"] or 0),
            "failed_plans": int(row["failed_plans"] or 0),
            "completed_plans": int(row["completed_plans"] or 0),
            "pending_reviews": int(pending_reviews or 0),
        }

    def list_reviews(self, *, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            reviews = [
                dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT review_id, isbn, candidates_json, evidence_json,
                               keep_md5s_json, status, created_at, updated_at, decided_at
                        FROM library_isbn_duplicate_reviews
                        WHERE (:status = '' OR status = :status)
                        ORDER BY review_id DESC LIMIT :limit
                        """
                    ),
                    {"status": status, "limit": max(1, min(int(limit), 500))},
                ).mappings()
            ]
            candidate_md5s = sorted(
                {
                    str(candidate.get("md5") or "").strip().lower()
                    for review in reviews
                    for candidate in review.get("candidates_json") or []
                    if candidate.get("md5")
                }
            )
            if not candidate_md5s:
                return reviews
            metadata_rows = conn.execute(
                text("SELECT md5, schema_org FROM metadata WHERE md5 = ANY(:md5s)"),
                {"md5s": candidate_md5s},
            ).mappings()
            page_counts = {
                str(row["md5"]).strip().lower(): extract_page_count(
                    parse_meta(row.get("schema_org"))
                )
                for row in metadata_rows
            }
            return _enrich_review_page_counts(reviews, page_counts)

    def decide_review(self, review_id: int, *, keep_md5s: Iterable[str]) -> dict[str, Any]:
        keep = tuple(sorted({str(value).strip().lower() for value in keep_md5s if value}))
        if not keep:
            raise ValueError("At least one document must be kept")
        with self.engine.begin() as conn:
            review = conn.execute(
                text(
                    """
                    SELECT review_id, isbn, candidates_json, keep_md5s_json, status
                    FROM library_isbn_duplicate_reviews
                    WHERE review_id = :review_id FOR UPDATE
                    """
                ),
                {"review_id": int(review_id)},
            ).mappings().one_or_none()
            if review is None:
                raise ValueError("ISBN review not found")
            candidates = list(review["candidates_json"] or [])
            by_md5 = {str(item.get("md5") or "").lower(): dict(item) for item in candidates}
            if not set(keep).issubset(by_md5):
                raise ValueError("keep_md5s contains a document outside this review")
            status = str(review["status"])
            existing_keep = tuple(
                sorted(
                    str(value).strip().lower()
                    for value in review["keep_md5s_json"] or []
                    if value
                )
            )
            if status == "decided" and keep != existing_keep:
                raise ValueError("ISBN review is no longer pending")
            if status not in {"pending", "decided"}:
                raise ValueError("ISBN review is no longer pending")
            if status == "pending":
                conn.execute(
                    text(
                        """
                        UPDATE library_isbn_duplicate_reviews SET
                            keep_md5s_json = CAST(:keep_json AS JSONB),
                            status = 'decided', decided_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE review_id = :review_id
                        """
                    ),
                    {"review_id": int(review_id), "keep_json": json.dumps(keep)},
                )
        return {
            "review_id": int(review_id),
            "isbn": str(review["isbn"]),
            "keep_md5s": list(keep),
            "remove_candidates": [item for md5, item in by_md5.items() if md5 not in keep],
        }

    def undo_review(self, review_id: int) -> dict[str, Any]:
        """Reopen one decision when its exclusive cleanup plans remain reversible."""
        normalized_review_id = int(review_id)
        with self.engine.begin() as conn:
            review = conn.execute(
                text(
                    """
                    SELECT review_id, isbn, candidates_json, keep_md5s_json, status
                    FROM library_isbn_duplicate_reviews
                    WHERE review_id=:review_id FOR UPDATE
                    """
                ),
                {"review_id": normalized_review_id},
            ).mappings().one_or_none()
            if review is None:
                raise ValueError("ISBN review not found")
            if str(review["status"]) != "decided":
                raise ValueError("Only a decided ISBN review can be undone")

            removed_md5s = sorted(_removed_review_md5s(review))
            other_decisions = conn.execute(
                text(
                    """
                    SELECT review_id, candidates_json, keep_md5s_json
                    FROM library_isbn_duplicate_reviews
                    WHERE status='decided' AND review_id<>:review_id
                    FOR SHARE
                    """
                ),
                {"review_id": normalized_review_id},
            ).mappings()
            shared_removed_md5s = _shared_removed_md5s(other_decisions)

            plans = []
            if removed_md5s:
                plans = list(
                    conn.execute(
                        text(
                            """
                            SELECT cleanup_id, md5, status, evidence_json
                            FROM document_cleanup_queue
                            WHERE reason='duplicate_isbn' AND md5=ANY(:md5s)
                            FOR UPDATE
                            """
                        ),
                        {"md5s": removed_md5s},
                    ).mappings()
                )
            exclusive_plans = [
                plan
                for plan in plans
                if int((plan["evidence_json"] or {}).get("review_id") or 0)
                == normalized_review_id
                and str(plan["md5"]).strip().lower() not in shared_removed_md5s
            ]
            if any(
                str(plan["status"]) in {"running", "completed", "recovered"}
                for plan in exclusive_plans
            ):
                raise ValueError(
                    "Cleanup has already started; this ISBN decision can no longer be undone"
                )
            cancel_ids = sorted(
                int(plan["cleanup_id"])
                for plan in exclusive_plans
                if str(plan["status"]) in {"planned", "failed"}
            )
            if cancel_ids:
                conn.execute(
                    text(
                        """
                        UPDATE document_cleanup_queue SET
                            status='canceled', phase='canceled', last_error=NULL,
                            completed_at=COALESCE(completed_at, CURRENT_TIMESTAMP),
                            updated_at=CURRENT_TIMESTAMP,
                            evidence_json=evidence_json || jsonb_build_object(
                                'cancellation', 'isbn_review_undo'
                            )
                        WHERE cleanup_id=ANY(:cleanup_ids)
                        """
                    ),
                    {"cleanup_ids": cancel_ids},
                )
            conn.execute(
                text(
                    """
                    UPDATE library_isbn_duplicate_reviews SET
                        keep_md5s_json='[]'::jsonb, status='pending',
                        decided_at=NULL, updated_at=CURRENT_TIMESTAMP
                    WHERE review_id=:review_id
                    """
                ),
                {"review_id": normalized_review_id},
            )
        return {
            "review_id": normalized_review_id,
            "isbn": str(review["isbn"]),
            "status": "pending",
            "canceled_cleanup_ids": cancel_ids,
        }


__all__ = ["DocumentCleanupRepository"]
