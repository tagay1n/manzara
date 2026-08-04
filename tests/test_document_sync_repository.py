"""SQL contract tests for document synchronization persistence."""

from __future__ import annotations

from app.modules.maintenance.document_sync_repository import (
    PostgresDocumentSyncRepository,
)


class _Rows:
    def mappings(self):
        return self

    def all(self):
        return []


class _Connection:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, _parameters=None):  # noqa: ANN001
        self.statements.append(str(statement))
        return _Rows()


class _Engine:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def connect(self):
        return _Connection(self.statements)

    def begin(self):
        return _Connection(self.statements)


def test_repository_quotes_reserved_full_column_in_all_sql() -> None:
    repository = PostgresDocumentSyncRepository.__new__(
        PostgresDocumentSyncRepository
    )
    repository.engine = _Engine()

    repository.list_documents()
    repository.save_verified_document(
        {
            "md5": "a" * 32,
            "mime_type": "application/pdf",
            "ya_path": "/book.pdf",
            "ya_public_url": None,
            "ya_public_key": None,
            "ya_resource_id": None,
            "full": True,
            "sharing_restricted": False,
            "document_url": "https://example.test/book.pdf",
            "upstream_meta_url": None,
            "primary_storage_size": 1,
            "primary_storage_etag": "etag",
            "primary_storage_verified_at": "2026-08-04T00:00:00+00:00",
            "created": False,
        }
    )

    select_sql, upsert_sql = repository.engine.statements
    assert 'language, "full", sharing_restricted' in select_sql
    assert 'language, "full", sharing_restricted' in upsert_sql
    assert '"full" = EXCLUDED."full"' in upsert_sql
