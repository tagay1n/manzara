"""SQL contract tests for monocorpus cleanup persistence."""

from __future__ import annotations

from app.modules.maintenance.monocorpus_sync_repository import (
    MonocorpusSyncRepository,
)


class _Result:
    rowcount = 1

    def scalar_one(self) -> bool:
        return True


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


def test_document_cleanup_does_not_reference_retired_crh_tables() -> None:
    repository = MonocorpusSyncRepository.__new__(MonocorpusSyncRepository)
    repository.engine = _Engine()

    repository.delete_document_state("a" * 32)

    sql = "\n".join(repository.engine.statements)
    assert "_crh" not in sql
    assert 'DELETE FROM "library_non_pdf_extraction_state" WHERE md5=:md5' in sql
    assert 'DELETE FROM "library_metadata_extraction_state" WHERE md5=:md5' in sql
    assert "DELETE FROM document WHERE md5=:md5" in sql


def test_catalog_update_clears_storage_checkpoint_when_source_identity_changes() -> None:
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
        }
    )

    sql = repository.engine.statements[0]
    assert "ya_path IS DISTINCT FROM :ya_path" in sql
    assert "mime_type IS DISTINCT FROM :mime_type" in sql
    assert "sharing_restricted IS DISTINCT FROM :sharing_restricted" in sql
    assert "THEN NULL ELSE document_url END" in sql
    assert "THEN NULL ELSE primary_storage_verified_at END" in sql
