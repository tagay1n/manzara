"""Alembic coverage for guarded document cleanup persistence."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from app.modules.library.document_cleanup_repository import DocumentCleanupRepository


def test_document_cleanup_tables_are_migrated(prepared_test_schema) -> None:
    database_url, schema = prepared_test_schema
    from sqlalchemy import create_engine

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert inspector.has_table("document_cleanup_queue", schema=schema)
        assert inspector.has_table("library_isbn_duplicate_reviews", schema=schema)
        queue_columns = {
            item["name"]
            for item in inspector.get_columns("document_cleanup_queue", schema=schema)
        }
        assert {"cleanup_id", "phase", "evidence_json", "last_error"}.issubset(
            queue_columns
        )
        with engine.begin() as conn:
            for status in ("canceled", "recovered"):
                conn.execute(
                    text(
                        f'''INSERT INTO "{schema}".document_cleanup_queue (
                            scope, action, reason, md5, source_path, status
                        ) VALUES (
                            'document', 'move', 'test', :md5, '/test', :status
                        )'''
                    ),
                    {"md5": status, "status": status},
                )
            conn.execute(
                text(
                    f'''INSERT INTO "{schema}".document_cleanup_queue (
                        scope, action, reason, md5, source_resource_id,
                        source_path, target_path
                    ) VALUES (
                        'source_resource', 'move', 'corrupted', :md5,
                        'resource:empty', '/documents/empty.pdf',
                        '/filtered/corrupted/empty.pdf'
                    )'''
                ),
                {"md5": "d41d8cd98f00b204e9800998ecf8427e"},
            )
    finally:
        engine.dispose()


def test_decided_isbn_review_can_be_undone_before_cleanup_starts(
    prepared_test_schema,
) -> None:
    database_url, schema = prepared_test_schema
    repository = DocumentCleanupRepository(database_url, schema=schema)
    md5 = "a" * 32
    try:
        with repository.engine.begin() as conn:
            review_id = conn.execute(
                text(
                    """
                    INSERT INTO library_isbn_duplicate_reviews (
                        isbn, candidate_hash, candidates_json, keep_md5s_json,
                        status, decided_at
                    ) VALUES (
                        '9780306406157', 'undo-test',
                        CAST(:candidates AS JSONB), CAST(:keep AS JSONB),
                        'decided', CURRENT_TIMESTAMP
                    ) RETURNING review_id
                    """
                ),
                {
                    "candidates": '[{"md5":"' + md5 + '"}]',
                    "keep": "[]",
                },
            ).scalar_one()
            cleanup_id = conn.execute(
                text(
                    """
                    INSERT INTO document_cleanup_queue (
                        scope, action, reason, md5, source_path, target_path,
                        evidence_json
                    ) VALUES (
                        'document', 'move', 'duplicate_isbn', :md5,
                        '/books/a.pdf', '/filtered/a.pdf',
                        jsonb_build_object('review_id', :review_id)
                    ) RETURNING cleanup_id
                    """
                ),
                {"md5": md5, "review_id": review_id},
            ).scalar_one()

        result = repository.undo_review(review_id)

        assert result["status"] == "pending"
        assert result["canceled_cleanup_ids"] == [cleanup_id]
        with repository.engine.connect() as conn:
            review = conn.execute(
                text(
                    "SELECT status, keep_md5s_json, decided_at "
                    "FROM library_isbn_duplicate_reviews WHERE review_id=:review_id"
                ),
                {"review_id": review_id},
            ).mappings().one()
            cleanup_status = conn.execute(
                text(
                    "SELECT status FROM document_cleanup_queue "
                    "WHERE cleanup_id=:cleanup_id"
                ),
                {"cleanup_id": cleanup_id},
            ).scalar_one()
        assert review["status"] == "pending"
        assert review["keep_md5s_json"] == []
        assert review["decided_at"] is None
        assert cleanup_status == "canceled"
    finally:
        repository.dispose()


def test_isbn_review_undo_refuses_started_cleanup(prepared_test_schema) -> None:
    database_url, schema = prepared_test_schema
    repository = DocumentCleanupRepository(database_url, schema=schema)
    md5 = "b" * 32
    try:
        with repository.engine.begin() as conn:
            review_id = conn.execute(
                text(
                    """
                    INSERT INTO library_isbn_duplicate_reviews (
                        isbn, candidate_hash, candidates_json, status, decided_at
                    ) VALUES (
                        '9781861972712', 'undo-running-test',
                        CAST(:candidates AS JSONB), 'decided', CURRENT_TIMESTAMP
                    ) RETURNING review_id
                    """
                ),
                {"candidates": '[{"md5":"' + md5 + '"}]'},
            ).scalar_one()
            conn.execute(
                text(
                    """
                    INSERT INTO document_cleanup_queue (
                        scope, action, reason, md5, source_path, target_path,
                        status, evidence_json
                    ) VALUES (
                        'document', 'move', 'duplicate_isbn', :md5,
                        '/books/b.pdf', '/filtered/b.pdf', 'running',
                        jsonb_build_object('review_id', :review_id)
                    )
                    """
                ),
                {"md5": md5, "review_id": review_id},
            )

        with pytest.raises(ValueError, match="already started"):
            repository.undo_review(review_id)

        with repository.engine.connect() as conn:
            status = conn.execute(
                text(
                    "SELECT status FROM library_isbn_duplicate_reviews "
                    "WHERE review_id=:review_id"
                ),
                {"review_id": review_id},
            ).scalar_one()
        assert status == "decided"
    finally:
        repository.dispose()


def test_isbn_review_undo_reopens_keep_all_decision_without_cleanup_plan(
    prepared_test_schema,
) -> None:
    database_url, schema = prepared_test_schema
    repository = DocumentCleanupRepository(database_url, schema=schema)
    md5 = "d" * 32
    try:
        with repository.engine.begin() as conn:
            review_id = conn.execute(
                text(
                    """
                    INSERT INTO library_isbn_duplicate_reviews (
                        isbn, candidate_hash, candidates_json, keep_md5s_json,
                        status, decided_at
                    ) VALUES (
                        '9780743273565', 'undo-keep-all-test',
                        CAST(:candidates AS JSONB), CAST(:keep AS JSONB),
                        'decided', CURRENT_TIMESTAMP
                    ) RETURNING review_id
                    """
                ),
                {
                    "candidates": '[{"md5":"' + md5 + '"}]',
                    "keep": '["' + md5 + '"]',
                },
            ).scalar_one()

        result = repository.undo_review(review_id)

        assert result["status"] == "pending"
        assert result["canceled_cleanup_ids"] == []
    finally:
        repository.dispose()


def test_isbn_review_undo_preserves_cleanup_shared_by_another_decision(
    prepared_test_schema,
) -> None:
    database_url, schema = prepared_test_schema
    repository = DocumentCleanupRepository(database_url, schema=schema)
    md5 = "c" * 32
    try:
        with repository.engine.begin() as conn:
            review_ids = [
                conn.execute(
                    text(
                        """
                        INSERT INTO library_isbn_duplicate_reviews (
                            isbn, candidate_hash, candidates_json, status, decided_at
                        ) VALUES (
                            :isbn, :candidate_hash, CAST(:candidates AS JSONB),
                            'decided', CURRENT_TIMESTAMP
                        ) RETURNING review_id
                        """
                    ),
                    {
                        "isbn": isbn,
                        "candidate_hash": candidate_hash,
                        "candidates": '[{"md5":"' + md5 + '"}]',
                    },
                ).scalar_one()
                for isbn, candidate_hash in (
                    ("9780140328721", "undo-shared-a"),
                    ("9780061120084", "undo-shared-b"),
                )
            ]
            cleanup_id = conn.execute(
                text(
                    """
                    INSERT INTO document_cleanup_queue (
                        scope, action, reason, md5, source_path, target_path,
                        evidence_json
                    ) VALUES (
                        'document', 'move', 'duplicate_isbn', :md5,
                        '/books/c.pdf', '/filtered/c.pdf',
                        jsonb_build_object('review_id', :review_id)
                    ) RETURNING cleanup_id
                    """
                ),
                {"md5": md5, "review_id": review_ids[0]},
            ).scalar_one()

        result = repository.undo_review(review_ids[0])

        assert result["canceled_cleanup_ids"] == []
        with repository.engine.connect() as conn:
            cleanup_status = conn.execute(
                text(
                    "SELECT status FROM document_cleanup_queue "
                    "WHERE cleanup_id=:cleanup_id"
                ),
                {"cleanup_id": cleanup_id},
            ).scalar_one()
        assert cleanup_status == "planned"
    finally:
        repository.dispose()


def test_isbn_review_decision_retry_with_same_selection_is_idempotent(
    prepared_test_schema,
) -> None:
    database_url, schema = prepared_test_schema
    repository = DocumentCleanupRepository(database_url, schema=schema)
    kept_md5 = "e" * 32
    removed_md5 = "f" * 32
    try:
        with repository.engine.begin() as conn:
            review_id = conn.execute(
                text(
                    """
                    INSERT INTO library_isbn_duplicate_reviews (
                        isbn, candidate_hash, candidates_json
                    ) VALUES (
                        '9780679760801', 'decision-retry-test',
                        CAST(:candidates AS JSONB)
                    ) RETURNING review_id
                    """
                ),
                {
                    "candidates": (
                        '[{"md5":"' + kept_md5 + '"},'
                        '{"md5":"' + removed_md5 + '"}]'
                    ),
                },
            ).scalar_one()

        first = repository.decide_review(review_id, keep_md5s=[kept_md5])
        retried = repository.decide_review(review_id, keep_md5s=[kept_md5])

        assert retried == first
        with pytest.raises(ValueError, match="no longer pending"):
            repository.decide_review(review_id, keep_md5s=[removed_md5])
    finally:
        repository.dispose()
