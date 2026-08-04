"""SQL contract tests for document synchronization persistence."""

from __future__ import annotations

import pytest

from app.modules.maintenance.document_sync_repository import (
    PostgresDocumentSyncRepository,
)


class _Result:
    def __init__(self, rowcount: int = 0, rows: list[dict] | None = None) -> None:
        self.rowcount = rowcount
        self.rows = rows or []

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _Connection:
    def __init__(self, engine: "_Engine") -> None:
        self.engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, _parameters=None):  # noqa: ANN001
        sql = str(statement)
        self.engine.statements.append(sql)
        if sql.lstrip().startswith("SELECT"):
            return _Result(rows=self.engine.rows)
        rowcount = self.engine.rowcounts.pop(0) if self.engine.rowcounts else 0
        return _Result(rowcount)


class _Engine:
    def __init__(self, *rowcounts: int, rows: list[dict] | None = None) -> None:
        self.statements: list[str] = []
        self.rowcounts = list(rowcounts)
        self.rows = rows or []

    def connect(self):
        return _Connection(self)

    def begin(self):
        return _Connection(self)


def _payload() -> dict:
    return {
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


def _repository(*rowcounts: int) -> PostgresDocumentSyncRepository:
    repository = PostgresDocumentSyncRepository.__new__(
        PostgresDocumentSyncRepository
    )
    repository.engine = _Engine(*rowcounts)
    return repository


def test_repository_updates_existing_md5_without_on_conflict() -> None:
    repository = _repository(1)

    created = repository.save_verified_document(_payload())

    assert created is False
    assert len(repository.engine.statements) == 1
    update_sql = repository.engine.statements[0]
    assert update_sql.lstrip().startswith("UPDATE document")
    assert '"full" = :full' in update_sql
    assert "WHERE md5 = :md5" in update_sql
    assert "ON CONFLICT" not in update_sql


def test_repository_inserts_only_when_md5_does_not_exist() -> None:
    repository = _repository(0, 1)

    created = repository.save_verified_document(_payload())

    assert created is True
    assert len(repository.engine.statements) == 2
    assert repository.engine.statements[1].lstrip().startswith("INSERT INTO document")
    assert 'language, "full", sharing_restricted' in repository.engine.statements[1]


def test_repository_rejects_ambiguous_duplicate_md5_and_does_not_insert() -> None:
    repository = _repository(2)

    with pytest.raises(RuntimeError, match="matched 2 rows"):
        repository.save_verified_document(_payload())

    assert len(repository.engine.statements) == 1


def test_repository_quotes_reserved_full_column_in_select() -> None:
    repository = _repository()

    repository.list_documents()

    assert 'language, "full", sharing_restricted' in repository.engine.statements[0]


def test_repository_rejects_duplicate_md5_during_initial_read() -> None:
    repository = _repository()
    repository.engine.rows = [
        {"md5": "a" * 32},
        {"md5": "a" * 32},
    ]

    with pytest.raises(RuntimeError, match="Duplicate document MD5"):
        repository.list_documents()
