"""PostgreSQL persistence for document cleanup plans and ISBN reviews."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import text

from app.catalog.isbn import equivalent_isbn_values
from app.catalog.contracts import integer
from app.operational_state import configured_store
from app.postgres_engine import acquire_postgres_engine, release_postgres_engine
from app.document_operation_lock import lock_document_transaction


_ISBN_REVIEW_LOCK = text(
    "SELECT pg_advisory_xact_lock(hashtext(current_schema()), "
    "hashtext('library_isbn_duplicate_review_mutation'))"
)


def _lock_isbn_reviews(conn: Any) -> None:
    """Serialize review identity and decision mutations within one schema."""
    conn.execute(_ISBN_REVIEW_LOCK)


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


def review_snapshot(candidates: Iterable[Mapping[str, Any]]) -> str:
    payload = json.dumps(list(candidates), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


def _candidate_md5s(candidates: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(candidate.get("md5") or "").strip().lower()
                for candidate in candidates
                if candidate.get("md5")
            }
        )
    )


def _review_identity_isbns(review: Mapping[str, Any]) -> set[str]:
    evidence = review.get("evidence_json") or {}
    matched = evidence.get("matched_isbns") if isinstance(evidence, Mapping) else []
    identities: set[str] = set()
    for identifier in [
        review.get("isbn"),
        *(matched if isinstance(matched, list) else []),
    ]:
        aliases = equivalent_isbn_values(identifier)
        identities.update(aliases or [str(identifier or "")])
    identities.discard("")
    return identities


def _reconcile_pending_reviews(conn: Any) -> dict[str, int]:
    """Remove missing candidates and supersede reviews that are no longer conflicts."""
    reviews = [
        dict(row)
        for row in conn.execute(
            text(
                """
                SELECT review_id, isbn, candidates_json, evidence_json
                FROM library_isbn_duplicate_reviews
                WHERE status='pending'
                ORDER BY review_id
                FOR UPDATE
                """
            )
        ).mappings()
    ]
    candidate_md5s = sorted(
        {
            md5
            for review in reviews
            for md5 in _candidate_md5s(review.get("candidates_json") or [])
        }
    )
    present_md5s: set[str] = set()
    if candidate_md5s:
        present_md5s = {
            str(value).strip().lower()
            for value in conn.execute(
                text("SELECT md5 FROM catalog_documents WHERE md5=ANY(:md5s)"),
                {"md5s": candidate_md5s},
            ).scalars()
        }

    superseded: dict[int, tuple[str, list[str]]] = {}
    survivors: list[tuple[dict[str, Any], list[dict[str, Any]], list[str]]] = []
    winners: dict[tuple[str, ...], list[tuple[int, set[str]]]] = {}
    for review in reviews:
        candidates = [dict(item) for item in review.get("candidates_json") or []]
        remaining = [
            item
            for item in candidates
            if str(item.get("md5") or "").strip().lower() in present_md5s
        ]
        removed = sorted(
            set(_candidate_md5s(candidates)) - set(_candidate_md5s(remaining))
        )
        review_id = int(review["review_id"])
        if len(_candidate_md5s(remaining)) < 2:
            superseded[review_id] = ("fewer_than_two_available_candidates", removed)
            continue
        candidate_key = _candidate_md5s(remaining)
        identities = _review_identity_isbns(review)
        duplicate_of = next(
            (
                winner_id
                for winner_id, winner_identities in winners.get(candidate_key, [])
                if identities & winner_identities
            ),
            None,
        )
        if duplicate_of is not None:
            superseded[review_id] = (f"duplicate_of_review_{duplicate_of}", removed)
            continue
        winners.setdefault(candidate_key, []).append((review_id, identities))
        survivors.append((review, remaining, removed))

    for review_id, (reason, removed) in superseded.items():
        conn.execute(
            text(
                """
                UPDATE library_isbn_duplicate_reviews SET
                    status='superseded', updated_at=CURRENT_TIMESTAMP,
                    evidence_json=evidence_json || jsonb_build_object(
                        'reconciliation', jsonb_build_object(
                            'reason', :reason,
                            'removed_missing_md5s', CAST(:removed_json AS JSONB)
                        )
                    )
                WHERE review_id=:review_id AND status='pending'
                """
            ),
            {
                "review_id": review_id,
                "reason": reason,
                "removed_json": json.dumps(removed),
            },
        )

    pruned = 0
    for review, remaining, removed in survivors:
        if not removed:
            continue
        conn.execute(
            text(
                """
                UPDATE library_isbn_duplicate_reviews SET
                    candidates_json=CAST(:candidates_json AS JSONB),
                    updated_at=CURRENT_TIMESTAMP,
                    evidence_json=evidence_json || jsonb_build_object(
                        'reconciliation', jsonb_build_object(
                            'reason', 'document_cleanup',
                            'removed_missing_md5s', CAST(:removed_json AS JSONB)
                        )
                    )
                WHERE review_id=:review_id AND status='pending'
                """
            ),
            {
                "review_id": int(review["review_id"]),
                "candidates_json": json.dumps(
                    remaining, ensure_ascii=False, sort_keys=True
                ),
                "removed_json": json.dumps(removed),
            },
        )
        pruned += 1
    return {"pruned": pruned, "superseded": len(superseded)}


class DocumentCleanupRepository:
    """Own cleanup planning, review, claiming, and status transitions."""

    def __init__(self, database_url: str, *, schema: str) -> None:
        self.engine = acquire_postgres_engine(database_url, schema=schema)

    def dispose(self) -> None:
        release_postgres_engine(self.engine)

    @contextmanager
    def write_transaction(self, *, conn: Any = None):
        """Opt cleanup mutations into writes without changing session defaults."""
        if conn is not None:
            yield conn
            return
        with self.engine.begin() as connection:
            connection.execute(text("SET TRANSACTION READ WRITE"))
            yield connection

    def list_documents_for_planning(self, *, conn: Any = None, md5s: Iterable[str] | None = None) -> list[dict[str, Any]]:
        where = "WHERE d.md5=ANY(:md5s)" if md5s is not None else ""
        parameters = {"md5s": list(md5s)} if md5s is not None else {}
        with nullcontext(conn) if conn is not None else self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(text(f"""
                SELECT d.md5, d.publication_id, d.revision AS document_revision,
                       p.revision AS publication_revision, y.revision AS source_revision,
                       d.mime_type, d.complete AS "full", d.restricted AS sharing_restricted,
                       y.source_path AS ya_path, y.resource_id AS ya_resource_id,
                       p.name AS title, p.page_count,
                       ARRAY(SELECT language FROM catalog_publication_languages l
                             WHERE l.publication_id=p.publication_id ORDER BY position) AS language,
                       ARRAY(SELECT value FROM catalog_identifiers i
                             WHERE i.publication_id=p.publication_id AND kind='isbn'
                             ORDER BY position) AS isbn
                FROM catalog_documents d
                JOIN catalog_publications p USING (publication_id)
                LEFT JOIN catalog_locations y
                  ON y.md5=d.md5 AND y.provider='yandex' AND y.purpose='source'
                {where}
                ORDER BY d.md5
            """), parameters).mappings()]

    def enqueue_cleanup(self, payload: Mapping[str, Any], *, conn: Any = None) -> tuple[int, bool]:
        values = {
            **dict(payload),
            "evidence_json": json.dumps(
                payload.get("evidence") or {}, ensure_ascii=False, sort_keys=True
            ),
        }
        with self.write_transaction(conn=conn) as conn:
            lock_document_transaction(conn, values['md5'])
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
                # Preparation may refresh unexecuted automatic decisions after
                # unrelated catalog edits; persisted targets remain immutable.
                if values['scope'] == 'document' and values['reason'] in {'non_tatar', 'non_document'}:
                    conn.execute(text("""UPDATE document_cleanup_queue SET
                        evidence_json=evidence_json || CAST(:evidence_json AS JSONB),
                        source_resource_id=:source_resource_id, status='planned',
                        updated_at=CURRENT_TIMESTAMP
                        WHERE cleanup_id=:cleanup_id AND phase='planned'
                          AND reason=:reason AND action=:action AND source_path=:source_path
                    """), {**values, 'cleanup_id': existing})
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
        candidate_md5s = _candidate_md5s(candidate_list)
        candidate_hash = hashlib.sha256(
            json.dumps(candidate_md5s).encode("utf-8")
        ).hexdigest()
        values = {
            "isbn": isbn,
            "candidate_hash": candidate_hash,
            "candidates_json": canonical,
            "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        }
        matched_isbns = evidence.get("matched_isbns")
        identity_isbns: set[str] = set()
        for identifier in [
            isbn,
            *(matched_isbns if isinstance(matched_isbns, list) else []),
        ]:
            aliases = equivalent_isbn_values(identifier)
            identity_isbns.update(aliases or [str(identifier)])
        with self.write_transaction() as conn:
            _lock_isbn_reviews(conn)
            possible_reviews = conn.execute(
                text(
                    """
                    SELECT review_id, isbn, status, candidates_json
                    FROM library_isbn_duplicate_reviews
                    WHERE isbn = ANY(:identity_isbns)
                    """
                ),
                {
                    **values,
                    "identity_isbns": sorted(identity_isbns),
                },
            ).mappings().all()
            existing_reviews = sorted(
                (
                    item
                    for item in possible_reviews
                    if _candidate_md5s(item["candidates_json"] or []) == candidate_md5s
                ),
                key=lambda item: (
                    {"decided": 0, "pending": 1}.get(str(item["status"]), 2),
                    str(item["isbn"]) != isbn,
                    int(item["review_id"]),
                ),
            )
            if existing_reviews:
                selected = existing_reviews[0]
                selected_id = int(selected["review_id"])
                redundant_pending_ids = [
                    int(item["review_id"])
                    for item in existing_reviews[1:]
                    if item["status"] == "pending"
                ]
                if redundant_pending_ids:
                    conn.execute(
                        text(
                            """
                            UPDATE library_isbn_duplicate_reviews
                            SET status = 'superseded', updated_at = CURRENT_TIMESTAMP
                            WHERE review_id = ANY(:review_ids) AND status = 'pending'
                            """
                        ),
                        {"review_ids": redundant_pending_ids},
                    )
                if selected["status"] == "pending":
                    conn.execute(
                        text(
                            """
                            UPDATE library_isbn_duplicate_reviews
                            SET candidates_json = CAST(:candidates_json AS JSONB),
                                evidence_json = CAST(:evidence_json AS JSONB),
                                updated_at = CURRENT_TIMESTAMP
                            WHERE review_id = :review_id
                            """
                        ),
                        {**values, "review_id": selected_id},
                    )
                return selected_id, False
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

    def reconcile_pending_reviews(self) -> dict[str, int]:
        """Persistently reconcile pending reviews with the current document catalog."""
        with self.write_transaction() as conn:
            self.lock_isbn_reviews_in_transaction(conn)
            return self.reconcile_pending_reviews_in_locked_transaction(conn)

    def lock_isbn_reviews_in_transaction(self, conn: Any) -> None:
        """Acquire the review mutation lock inside a caller-owned transaction."""
        _lock_isbn_reviews(conn)

    def reconcile_pending_reviews_in_locked_transaction(
        self, conn: Any
    ) -> dict[str, int]:
        """Reconcile reviews after the caller acquires the review mutation lock."""
        return _reconcile_pending_reviews(conn)

    def list_queue(self, *, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        where = "WHERE status = :status" if status else ""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT cleanup_id, scope, action, reason, md5,
                           source_resource_id, source_path, target_path, status,
                           phase, evidence_json,
                           created_at, updated_at, completed_at
                    FROM document_cleanup_queue
                    {where}
                    ORDER BY cleanup_id DESC LIMIT :limit
                    """
                ),
                {"status": status, "limit": max(1, min(int(limit), 500))},
            ).mappings()
            runtime = configured_store().list("maintenance.cleanup")
            return [{**dict(row), **runtime.get(str(row["cleanup_id"]), {})} for row in rows]


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
            for review in reviews:
                review["review_snapshot"] = review_snapshot(review["candidates_json"] or [])
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
            page_counts = dict(conn.execute(text("""
                SELECT d.md5, p.page_count FROM catalog_documents d
                JOIN catalog_publications p USING (publication_id)
                WHERE d.md5=ANY(:md5s)
            """), {"md5s": candidate_md5s}).all())
            return _enrich_review_page_counts(reviews, page_counts)

    def decide_review(self, review_id: int, *, keep_md5s: Iterable[str], expected_snapshot: str, conn: Any = None) -> dict[str, Any]:
        integer(review_id, "review_id")
        keep = tuple(sorted({str(value).strip().lower() for value in keep_md5s if value}))
        if not keep:
            raise ValueError("At least one document must be kept")
        with self.write_transaction(conn=conn) as conn:
            _lock_isbn_reviews(conn)
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
            if expected_snapshot != review_snapshot(candidates):
                raise ValueError("ISBN review changed; inspect cleanup reviews again")
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
                candidate_md5s = set(by_md5)
                # Lock the reviewed file/publication/source facts until plans commit.
                conn.execute(text("SELECT md5 FROM catalog_documents WHERE md5=ANY(:md5s) FOR UPDATE"),
                             {"md5s": sorted(candidate_md5s)})
                conn.execute(text("""SELECT publication_id FROM catalog_publications
                    WHERE publication_id IN (SELECT publication_id FROM catalog_documents
                                             WHERE md5=ANY(:md5s)) FOR UPDATE"""),
                             {"md5s": sorted(candidate_md5s)})
                conn.execute(text("SELECT location_id FROM catalog_locations WHERE md5=ANY(:md5s) FOR UPDATE"),
                             {"md5s": sorted(candidate_md5s)})
                current = {item["md5"]: item for item in self.list_documents_for_planning(conn=conn, md5s=candidate_md5s)}
                for md5, candidate in by_md5.items():
                    document = current.get(md5)
                    if document is None or any(candidate.get(key) != document.get(key) for key in
                        ("publication_id", "document_revision", "publication_revision", "source_revision")):
                        raise ValueError("ISBN candidates changed; rerun cleanup preparation before reviewing")
                    if candidate.get("source_path") != document["ya_path"]:
                        raise ValueError("ISBN source changed; rerun cleanup preparation")
                remove = candidate_md5s - set(keep)
                decided_reviews = conn.execute(
                    text(
                        """
                        SELECT review_id, candidates_json, keep_md5s_json
                        FROM library_isbn_duplicate_reviews
                        WHERE review_id <> :review_id
                          AND status = 'decided'
                          AND EXISTS (
                              SELECT 1
                              FROM jsonb_array_elements(candidates_json) candidate
                              WHERE candidate->>'md5' = ANY(:candidate_md5s)
                          )
                        ORDER BY review_id
                        """
                    ),
                    {
                        "review_id": int(review_id),
                        "candidate_md5s": sorted(candidate_md5s),
                    },
                ).mappings()
                for decided in decided_reviews:
                    other_candidates = {
                        str(item.get("md5") or "").strip().lower()
                        for item in decided["candidates_json"] or []
                        if item.get("md5")
                    }
                    other_keep = {
                        str(value).strip().lower()
                        for value in decided["keep_md5s_json"] or []
                        if value
                    }
                    other_remove = other_candidates - other_keep
                    conflicting = (set(keep) & other_remove) | (remove & other_keep)
                    if conflicting:
                        identifiers = ", ".join(sorted(conflicting))
                        raise ValueError(
                            "Decision conflicts with decided ISBN review "
                            f"{int(decided['review_id'])} for document(s): {identifiers}"
                        )
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
            "remove_candidates": [item for md5, item in by_md5.items() if md5 not in keep] if status == "pending" else [],
        }

    def undo_review(self, review_id: int) -> dict[str, Any]:
        """Reopen one decision when its exclusive cleanup plans remain reversible."""
        normalized_review_id = integer(review_id, "review_id")
        with self.write_transaction() as conn:
            _lock_isbn_reviews(conn)
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
                            SELECT cleanup_id, md5, status, phase, evidence_json
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
                str(plan["status"]) in {"running", "completed", "recovered"} or str(plan["phase"]) != "planned"
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
                            status='canceled', phase='canceled',
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
        store = configured_store()
        for cleanup_id in cancel_ids:
            previous = store.get("maintenance.cleanup", cleanup_id) or {}
            store.put("maintenance.cleanup", cleanup_id, {**previous, "last_error": None})
        return {
            "review_id": normalized_review_id,
            "isbn": str(review["isbn"]),
            "status": "pending",
            "canceled_cleanup_ids": cancel_ids,
        }


__all__ = ["DocumentCleanupRepository"]
