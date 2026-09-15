"""Library non pdf repository coverage."""

from __future__ import annotations

from app.modules.library.non_pdf_repository import NonPdfExtractionRepository


class _Rows:
    def __init__(self, rows) -> None:  # noqa: ANN001
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _Connection:
    def __init__(self, engine) -> None:  # noqa: ANN001
        self.engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, params):  # noqa: ANN001
        self.engine.sql = str(statement)
        self.engine.params = dict(params)
        return _Rows(self.engine.rows)


class _Engine:
    def __init__(self, rows=None) -> None:  # noqa: ANN001
        self.rows = list(rows or [])
        self.sql = ""
        self.params = {}

    def connect(self):
        return _Connection(self)

    def begin(self):
        return _Connection(self)


def test_candidate_queue_backfills_legacy_content_and_versions_unsupported() -> None:
    repository = NonPdfExtractionRepository.__new__(NonPdfExtractionRepository)
    repository.engine = _Engine()

    repository.list_candidates(
        extractor_version="nonpdf.v7", limit=10, per_mime_limit=100
    )

    assert "AND d.content_url IS NULL" not in repository.engine.sql
    assert "state.extractor_version IS DISTINCT FROM" in repository.engine.sql
    assert "state.status = 'processing'" in repository.engine.sql
    assert "state.status = 'failed'" in repository.engine.sql
    assert "state.attempt_count < :max_automatic_attempts" in repository.engine.sql
    assert "state.status = 'deferred'" in repository.engine.sql
    assert "primary_storage_verified_at IS NOT NULL" in repository.engine.sql
    assert "WHEN state.md5 IS NULL THEN 0" in repository.engine.sql
    assert "WHEN state.status = 'failed' THEN 3" in repository.engine.sql
    assert "CASE WHEN d.content_url IS NULL THEN 0 ELSE 1 END" in repository.engine.sql
    assert "ROW_NUMBER() OVER" in repository.engine.sql
    assert "d.mime_rank <=" in repository.engine.sql
    assert repository.engine.params["per_mime_limit"] == 100
    assert repository.engine.params["max_automatic_attempts"] == 3
    assert repository.engine.params["retry_known_failures"] is False


def test_candidate_queue_can_explicitly_retry_known_failures() -> None:
    repository = NonPdfExtractionRepository.__new__(NonPdfExtractionRepository)
    repository.engine = _Engine()

    repository.list_candidates(extractor_version="nonpdf.v7", retry_known_failures=True)

    assert repository.engine.params["retry_known_failures"] is True
    assert "cleanup.reason = 'corrupted'" in repository.engine.sql


def test_new_extractor_version_resets_automatic_attempt_budget() -> None:
    repository = NonPdfExtractionRepository.__new__(NonPdfExtractionRepository)
    repository.engine = _Engine()

    repository.start_attempt("a" * 32, extractor_version="nonpdf.v8", run_id=9)

    assert "IS DISTINCT FROM EXCLUDED.extractor_version" in repository.engine.sql
    assert "THEN 1" in repository.engine.sql
