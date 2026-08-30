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
        if sql.lstrip().startswith(("SELECT", "WITH")):
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


def test_repository_updates_only_storage_checkpoint_for_pending_md5() -> None:
    repository = _repository(1)

    updated = repository.save_storage_checkpoint(
        "a" * 32,
        _payload(),
        expected={
            "ya_path": "/book.pdf",
            "mime_type": "application/pdf",
            "sharing_restricted": False,
        },
    )

    assert updated is True
    assert len(repository.engine.statements) == 1
    update_sql = repository.engine.statements[0]
    assert update_sql.lstrip().startswith("UPDATE document")
    assert "document_url = :document_url" in update_sql
    assert "primary_storage_verified_at = :primary_storage_verified_at" in update_sql
    assert "mime_type = :mime_type" not in update_sql
    assert "WHERE md5 = :md5" in update_sql
    assert "document_url IS NULL" in update_sql
    assert "ya_path IS NOT DISTINCT FROM :expected_ya_path" in update_sql
    assert "mime_type IS NOT DISTINCT FROM :expected_mime_type" in update_sql
    assert "sharing_restricted IS NOT DISTINCT FROM" in update_sql


def test_repository_rejects_ambiguous_checkpoint_update() -> None:
    repository = _repository(2)

    with pytest.raises(RuntimeError, match="matched 2 rows"):
        repository.save_storage_checkpoint(
            "a" * 32,
            _payload(),
            expected={
                "ya_path": "/book.pdf",
                "mime_type": "application/pdf",
                "sharing_restricted": False,
            },
        )

    assert len(repository.engine.statements) == 1


def test_repository_selects_only_pending_documents_in_stable_order() -> None:
    repository = _repository()

    repository.list_pending_documents()

    assert "md5, mime_type, ya_path, sharing_restricted" in repository.engine.statements[0]
    assert "document_url IS NULL" in repository.engine.statements[0]
    assert "BTRIM(document_url) = ''" in repository.engine.statements[0]
    assert "ORDER BY ya_path NULLS LAST, md5" in repository.engine.statements[0]


def test_repository_rejects_duplicate_pending_md5_during_initial_read() -> None:
    repository = _repository()
    repository.engine.rows = [
        {"md5": "a" * 32},
        {"md5": "a" * 32},
    ]

    with pytest.raises(RuntimeError, match="Duplicate document MD5"):
        repository.list_pending_documents()
