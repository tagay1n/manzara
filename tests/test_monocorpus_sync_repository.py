"""SQL contract tests for monocorpus cleanup persistence."""

from __future__ import annotations

from app.modules.maintenance.monocorpus_sync_repository import (
    MonocorpusSyncRepository,
)


class _Result:
    rowcount = 1

    def mappings(self):
        return []

    def scalar_one(self) -> bool:
        return True

    def scalar_one_or_none(self) -> int:
        return 1


class _Connection:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, _parameters=None):  # noqa: ANN001
        self.statements.append(str(statement))
        return _Result()


class _Engine:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def begin(self) -> _Connection:
        return _Connection(self.statements)


def test_document_cleanup_explicitly_deletes_upstream_metadata_before_document() -> (
    None
):
    repository = MonocorpusSyncRepository.__new__(MonocorpusSyncRepository)
    repository.engine = _Engine()

    repository.delete_document_state("a" * 32)

    assert [statement.strip() for statement in repository.engine.statements] == [
        "SELECT pg_advisory_xact_lock(hashtext(current_schema()), "
        "hashtext('library_isbn_duplicate_review_mutation'))",
        "DELETE FROM library_upstream_metadata WHERE md5=:md5",
        "DELETE FROM document WHERE md5=:md5",
        "SELECT review_id, isbn, candidates_json, evidence_json\n"
        "                FROM library_isbn_duplicate_reviews\n"
        "                WHERE status='pending'\n"
        "                ORDER BY review_id\n"
        "                FOR UPDATE",
    ]


def test_cleanup_claim_excludes_plans_canceled_by_review_undo() -> None:
    repository = MonocorpusSyncRepository.__new__(MonocorpusSyncRepository)
    repository.engine = _Engine()

    assert repository.mark_cleanup_running(7, run_id=4, phase="yandex") is True

    sql = repository.engine.statements[0]
    assert "status IN ('planned', 'running', 'failed')" in sql
    assert "RETURNING cleanup_id" in sql


def test_catalog_update_clears_storage_checkpoint_only_when_target_changes() -> None:
    repository = MonocorpusSyncRepository.__new__(MonocorpusSyncRepository)
    repository.engine = _Engine()

    repository.save_discovered_document(
        {
            "md5": "a" * 32,
            "mime_type": "application/pdf",
            "ya_path": "/documents/book.pdf",
            "ya_public_url": None,
            "ya_public_key": None,
            "ya_resource_id": None,
            "full": True,
            "sharing_restricted": False,
        },
        reset_primary_storage=False,
    )

    sql = repository.engine.statements[0]
    assert "WHEN :reset_primary_storage" in sql
    assert "ya_path IS DISTINCT FROM :ya_path" not in sql
    assert "THEN NULL ELSE document_url END" in sql
    assert "THEN NULL ELSE primary_storage_verified_at END" in sql


def test_restricted_catalog_update_clears_persisted_public_links(
    test_database_url,
) -> None:
    import uuid
    from sqlalchemy import create_engine
    from sqlalchemy import text

    schema = "sharing_test_" + uuid.uuid4().hex
    setup_engine = create_engine(test_database_url)
    with setup_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(
            text(f'''CREATE TABLE "{schema}".document (
            md5 TEXT PRIMARY KEY, mime_type TEXT, ya_path TEXT,
            ya_public_url TEXT, ya_public_key TEXT, ya_resource_id TEXT,
            "full" BOOLEAN, sharing_restricted BOOLEAN, document_url TEXT,
            primary_storage_size BIGINT, primary_storage_etag TEXT,
            primary_storage_verified_at TIMESTAMPTZ
        )''')
        )
    repository = MonocorpusSyncRepository(test_database_url, schema=schema)
    md5 = "e" * 32
    payload = {
        "md5": md5,
        "mime_type": "application/pdf",
        "ya_path": "/documents/private/book.pdf",
        "ya_public_url": None,
        "ya_public_key": None,
        "ya_resource_id": "resource:book",
        "full": True,
        "sharing_restricted": True,
    }
    try:
        # A previously public row moving into the restricted folder must lose
        # both public fields even though the incoming values are NULL.
        repository.save_discovered_document(
            {
                **payload,
                "sharing_restricted": False,
                "ya_public_url": "https://disk/stale",
                "ya_public_key": "stale-key",
            },
            reset_primary_storage=False,
        )
        with repository.engine.begin() as conn:
            conn.execute(
                text("UPDATE document SET document_url='enc:existing' WHERE md5=:md5"),
                {"md5": md5},
            )
        repository.save_discovered_document(payload, reset_primary_storage=False)
        with repository.engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT ya_public_url, ya_public_key, document_url FROM document WHERE md5=:md5"
                    ),
                    {"md5": md5},
                )
                .mappings()
                .one()
            )
        assert row["ya_public_url"] is None
        assert row["ya_public_key"] is None
        assert row["document_url"] == "enc:existing"
        # An unrestricted update with missing metadata still keeps known links.
        repository.save_discovered_document(
            {
                **payload,
                "sharing_restricted": False,
                "ya_public_url": "https://disk/public",
                "ya_public_key": "public-key",
            },
            reset_primary_storage=False,
        )
        repository.save_discovered_document(
            {**payload, "sharing_restricted": False}, reset_primary_storage=False
        )
        with repository.engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT ya_public_url, ya_public_key FROM document WHERE md5=:md5"
                    ),
                    {"md5": md5},
                )
                .mappings()
                .one()
            )
        assert row["ya_public_url"] == "https://disk/public"
        assert row["ya_public_key"] == "public-key"
    finally:
        repository.dispose()
        with setup_engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        setup_engine.dispose()
