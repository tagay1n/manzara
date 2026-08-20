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
    assert "DELETE FROM document WHERE md5=:md5" in sql
