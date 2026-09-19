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


def test_candidate_queue_prioritizes_existing_content_and_versions_unsupported() -> None:
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


def test_deferred_pptx_is_checkpointed_without_replacing_content(test_database_url):
    """Exercise actual queue SQL, rather than only inspecting its predicates."""
    import uuid
    from sqlalchemy import create_engine, text
    from app.modules.library.non_pdf_types import EXTRACTOR_VERSION

    schema = "pptx_checkpoint_" + uuid.uuid4().hex
    admin = create_engine(test_database_url)
    repository = None
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            conn.execute(text(f'SET search_path TO "{schema}"'))
            conn.execute(
                text("""CREATE TABLE document (
                md5 TEXT PRIMARY KEY,mime_type TEXT,ya_path TEXT,document_url TEXT,
                primary_storage_size BIGINT,primary_storage_verified_at TIMESTAMPTZ,
                content_url TEXT)""")
            )
            conn.execute(
                text("""CREATE TABLE document_cleanup_queue (
                scope TEXT,md5 TEXT,reason TEXT,status TEXT)""")
            )
            conn.execute(
                text("""CREATE TABLE library_non_pdf_extraction_state (
                md5 TEXT PRIMARY KEY,extractor_version TEXT,status TEXT,
                attempt_count INTEGER,last_run_id BIGINT,detected_format TEXT,
                error_text TEXT,generated_at TIMESTAMPTZ,created_at TIMESTAMPTZ,updated_at TIMESTAMPTZ)""")
            )
            conn.execute(
                text("""INSERT INTO document VALUES (
                :md5,'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                '/deck.pptx','https://example/deck.pptx',100,CURRENT_TIMESTAMP,
                'https://example/old.zip')"""),
                {"md5": "a" * 32},
            )
        repository = NonPdfExtractionRepository(test_database_url, schema=schema)
        candidate = repository.list_candidates(extractor_version=EXTRACTOR_VERSION)[0]
        repository.start_attempt(
            candidate.md5, extractor_version=EXTRACTOR_VERSION, run_id=71
        )
        repository.mark_outcome(
            candidate.md5,
            extractor_version=EXTRACTOR_VERSION,
            detected_format="pptx",
            status="deferred",
            run_id=71,
            error_text="pptx_slide_images: visible slides contain images",
        )
        assert repository.list_candidates(extractor_version=EXTRACTOR_VERSION) == []
        assert (
            len(
                repository.list_candidates(
                    extractor_version=EXTRACTOR_VERSION,
                    retry_known_failures=True,
                )
            )
            == 1
        )
        assert len(repository.list_candidates(extractor_version="next-recipe")) == 1
        with repository.engine.connect() as conn:
            assert (
                conn.execute(text("SELECT content_url FROM document")).scalar()
                == "https://example/old.zip"
            )
            assert (
                conn.execute(
                    text("SELECT error_text FROM library_non_pdf_extraction_state")
                )
                .scalar()
                .startswith("pptx_slide_images")
            )
    finally:
        if repository is not None:
            repository.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
