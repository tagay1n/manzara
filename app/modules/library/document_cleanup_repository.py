"""PostgreSQL persistence for document cleanup plans and ISBN reviews."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from sqlalchemy import create_engine, text


class DocumentCleanupRepository:
    """Own cleanup planning, review, claiming, and status transitions."""

    def __init__(self, database_url: str, *, schema: str) -> None:
        self.engine = create_engine(
            database_url,
            connect_args={"options": f"-csearch_path={schema},public"},
        )

    def dispose(self) -> None:
        self.engine.dispose()

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
            if str(values["scope"]) == "duplicate_resource":
                existing = conn.execute(
                    text(
                        """
                        SELECT cleanup_id FROM document_cleanup_queue
                        WHERE scope = 'duplicate_resource'
                          AND source_resource_id = :source_resource_id
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
            rows = conn.execute(
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
            return [dict(row) for row in rows]

    def decide_review(self, review_id: int, *, keep_md5s: Iterable[str]) -> dict[str, Any]:
        keep = tuple(sorted({str(value).strip().lower() for value in keep_md5s if value}))
        if not keep:
            raise ValueError("At least one document must be kept")
        with self.engine.begin() as conn:
            review = conn.execute(
                text(
                    """
                    SELECT review_id, isbn, candidates_json, status
                    FROM library_isbn_duplicate_reviews
                    WHERE review_id = :review_id FOR UPDATE
                    """
                ),
                {"review_id": int(review_id)},
            ).mappings().one_or_none()
            if review is None:
                raise ValueError("ISBN review not found")
            if str(review["status"]) != "pending":
                raise ValueError("ISBN review is no longer pending")
            candidates = list(review["candidates_json"] or [])
            by_md5 = {str(item.get("md5") or "").lower(): dict(item) for item in candidates}
            if not set(keep).issubset(by_md5):
                raise ValueError("keep_md5s contains a document outside this review")
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


__all__ = ["DocumentCleanupRepository"]
