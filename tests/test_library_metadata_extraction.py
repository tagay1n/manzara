"""Metadata extraction contracts adopted from monocorpus `meta`."""

from __future__ import annotations

import hashlib

import pytest

from app.modules.library.metadata_extraction import (
    MetadataExtractionRepository,
    build_pdf_prompt,
    build_text_prompt,
    select_pdf_pages,
)
from app.modules.library.metadata_normalization import normalize_base_schema_org
from app.modules.library.metadata_prompt import (
    DEFINE_META_PROMPT_BODY,
    DEFINE_META_PROMPT_NON_PDF_HEADER,
    DEFINE_META_PROMPT_PDF_HEADER,
    DEFINE_META_PROMPT_TT_FOOTER,
)


class _Rows:
    def __init__(self, rows=None) -> None:  # noqa: ANN001
        self.rows = list(rows or [])

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

    def execute(self, statement, _params=None):  # noqa: ANN001
        self.engine.statements.append(str(statement))
        return _Rows(self.engine.rows)


class _Engine:
    def __init__(self, rows=None) -> None:  # noqa: ANN001
        self.statements: list[str] = []
        self.rows = list(rows or [])

    def connect(self):
        return _Connection(self)


class _WriteResult:
    def __init__(self, *, rows=None, rowcount=0) -> None:
        self.rows = list(rows or [])
        self.rowcount = rowcount

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _WriteConnection:
    def __init__(self, engine) -> None:  # noqa: ANN001
        self.engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, _params=None):  # noqa: ANN001
        self.engine.statements.append(str(statement))
        return self.engine.results.pop(0)


class _WriteEngine:
    def __init__(self, *results) -> None:  # noqa: ANN002
        self.results = list(results)
        self.statements: list[str] = []

    def begin(self):
        return _WriteConnection(self)


def test_metadata_prompt_is_copied_verbatim_from_monocorpus() -> None:
    content = "".join(
        (
            DEFINE_META_PROMPT_PDF_HEADER,
            DEFINE_META_PROMPT_NON_PDF_HEADER,
            DEFINE_META_PROMPT_BODY,
            DEFINE_META_PROMPT_TT_FOOTER,
        )
    )
    assert hashlib.sha256(content.encode()).hexdigest() == (
        "c487dee1d75621d311c7cd10e0eeb1333a9274f0ec1e389f485d711486da6c2e"
    )


def test_prompt_builders_preserve_source_order() -> None:
    text_prompt = build_text_prompt("content")
    assert text_prompt == [
        {"text": DEFINE_META_PROMPT_NON_PDF_HEADER.format(n=7)},
        {"text": DEFINE_META_PROMPT_BODY},
        {"text": DEFINE_META_PROMPT_TT_FOOTER},
        {"text": "Now, extract metadata from the following extraction from the document"},
        {"text": "content"},
    ]

    pdf_prompt = build_pdf_prompt(8, upstream_metadata='{"title":"Book"}')
    assert pdf_prompt[0] == {"text": DEFINE_META_PROMPT_PDF_HEADER.format(n=4)}
    assert pdf_prompt[-1] == {"text": "Now, extract metadata from the following document"}
    assert any('"title":"Book"' in part["text"] for part in pdf_prompt)


def test_pdf_page_selection_never_duplicates_short_documents() -> None:
    assert select_pdf_pages(1) == [0]
    assert select_pdf_pages(2) == [0, 1]
    assert select_pdf_pages(8) == list(range(8))
    assert select_pdf_pages(10) == [0, 1, 2, 3, 6, 7, 8, 9]


def test_adopted_normalization_cleans_base_metadata() -> None:
    normalized = normalize_base_schema_org(
        {
            "@context": "https://schema.org",
            "@type": "Book",
            "name": "unknown",
            "description": "  Some   text ",
            "inLanguage": "tt-Cyrl, ru-Cyrl, tt-Cyrl",
            "numberOfPages": "500 pages",
            "isbn": ["978-5-298-02109-8", "invalid"],
            "about": [
                {"termCode": "821.512.145", "inDefinedTermSet": "UDC"},
                {"termCode": "059", "inDefinedTermSet": "DDC"},
            ],
        }
    )

    assert "name" not in normalized
    assert normalized["description"] == "Some text"
    assert normalized["inLanguage"] == "ru-Cyrl, tt-Cyrl"
    assert normalized["numberOfPages"] == 500
    assert normalized["isbn"] == ["9785298021098"]
    assert normalized["about"] == [
        {
            "@type": "DefinedTerm",
            "termCode": "821.512.145",
            "inDefinedTermSet": "UDC",
        }
    ]


def test_candidate_query_requires_verified_primary_storage() -> None:
    repository = MetadataExtractionRepository.__new__(MetadataExtractionRepository)
    repository.engine = _Engine()

    repository.list_candidates(limit=25)

    sql = repository.engine.statements[0]
    assert "primary_storage_verified_at IS NOT NULL" in sql
    assert "primary_storage_size IS NOT NULL" in sql
    assert "document_url IS NOT NULL" in sql
    assert "schema_org IS NULL" in sql
    assert "library_metadata_extraction_state" in sql
    assert "ya_public_url" not in sql


def test_candidate_query_excludes_terminal_failures_only() -> None:
    repository = MetadataExtractionRepository.__new__(MetadataExtractionRepository)
    repository.engine = _Engine()

    repository.list_candidates(limit=25)

    sql = repository.engine.statements[0]
    assert "state.status IS NULL OR state.status = 'partial'" in sql


def test_candidate_query_rejects_duplicate_document_md5() -> None:
    row = {
        "md5": "a" * 32,
        "mime_type": "application/pdf",
        "document_url": "https://s3.example/public/a.pdf",
        "content_url": None,
        "upstream_meta_url": None,
        "primary_storage_size": 1,
        "attempts_json": [],
    }
    repository = MetadataExtractionRepository.__new__(MetadataExtractionRepository)
    repository.engine = _Engine([row, row])

    with pytest.raises(RuntimeError, match="Duplicate document MD5"):
        repository.list_candidates()


def test_success_write_preserves_existing_non_null_metadata() -> None:
    repository = MetadataExtractionRepository.__new__(MetadataExtractionRepository)
    repository.engine = _WriteEngine(
        _WriteResult(rows=[{"md5": "a" * 32, "schema_org": {"name": "Existing"}}]),
        _WriteResult(rowcount=1),
    )

    stored = repository.save_success(
        "a" * 32,
        schema_org={"@context": "https://schema.org", "@type": "Book"},
        model_name="model",
    )

    assert stored is False
    assert len(repository.engine.statements) == 2
    assert not any("INSERT INTO metadata" in sql for sql in repository.engine.statements)
    assert not any("UPDATE document" in sql for sql in repository.engine.statements)


def test_success_write_rejects_ambiguous_document_md5() -> None:
    repository = MetadataExtractionRepository.__new__(MetadataExtractionRepository)
    repository.engine = _WriteEngine(
        _WriteResult(
            rows=[
                {"md5": "a" * 32, "schema_org": None},
                {"md5": "a" * 32, "schema_org": None},
            ]
        )
    )

    with pytest.raises(RuntimeError, match="matched 2 rows"):
        repository.save_success(
            "a" * 32,
            schema_org={"@context": "https://schema.org", "@type": "Book"},
            model_name="model",
        )
