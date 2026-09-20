"""Library metadata evaluation response coverage."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.gemini_model_pool import GeminiModelResponseError
from app.modules.library.runtime.metadata.evaluation_response import (
    _parse_evaluation_response,
)
from app.modules.library.runtime.metadata.evaluation_patch import _collect_patch_fields


def test_evaluation_response_requires_classification_only_when_applicable(
    evaluation_document,
) -> None:
    with pytest.raises(GeminiModelResponseError, match="classification"):
        _parse_evaluation_response(
            json.dumps({"applicable": True, "reason": "Tatar literary work"}),
            doc=evaluation_document(),
            config={"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}},
        )

    result = _parse_evaluation_response(
        json.dumps({"applicable": False, "reason": "not a library document"}),
        doc=evaluation_document(),
        config={"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}},
    )

    assert result.applicable is False
    assert result.reason == "not a library document"

    with pytest.raises(GeminiModelResponseError, match="reason"):
        _parse_evaluation_response(
            json.dumps({"applicable": False}),
            doc=evaluation_document(),
            config={"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}},
        )


def test_book_gap_selection_requests_only_missing_book_fields(evaluation_document) -> None:
    doc = evaluation_document()

    assert "isbn" in _collect_patch_fields(doc.schema_org)
    assert "numberOfPages" in _collect_patch_fields(doc.schema_org)

    populated = dict(doc.schema_org, isbn=["9780140328721"], numberOfPages=96)
    assert "isbn" not in _collect_patch_fields(populated)
    assert "numberOfPages" not in _collect_patch_fields(populated)


@pytest.mark.parametrize(
    "work_type", ["Newspaper", "PublicationIssue", "Article", None, ["Book"]]
)
def test_non_book_gap_selection_never_requests_book_fields(
    evaluation_document, work_type: str
) -> None:
    schema_org = dict(evaluation_document().schema_org, **{"@type": work_type})

    fields = _collect_patch_fields(schema_org)

    assert "isbn" not in fields
    assert "numberOfPages" not in fields


def test_book_null_book_fields_are_accepted(evaluation_document) -> None:
    result = _parse_evaluation_response(
        json.dumps(
            {
                "applicable": False,
                "reason": "Utility document",
                "metadata_patch": {"isbn": None, "numberOfPages": None},
            }
        ),
        doc=evaluation_document(),
        config={"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}},
    )

    assert result.metadata_patch is None


def test_book_existing_book_fields_are_preserved(evaluation_document) -> None:
    doc = replace(
        evaluation_document(),
        schema_org=dict(
            evaluation_document().schema_org,
            isbn=["9780140328721"],
            numberOfPages=96,
        ),
    )

    result = _parse_evaluation_response(
        json.dumps(
            {
                "applicable": False,
                "reason": "Utility document",
                "metadata_patch": {
                    "isbn": ["9780439064873"],
                    "numberOfPages": 100,
                },
            }
        ),
        doc=doc,
        config={"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}},
    )

    assert result.metadata_patch is None


def test_non_book_response_discards_book_fields_and_applies_common_patch(
    evaluation_document,
) -> None:
    doc = replace(
        evaluation_document(),
        schema_org={
            "@context": "https://schema.org",
            "@type": "Newspaper",
            "name": "Existing title",
        },
    )

    result = _parse_evaluation_response(
        json.dumps(
            {
                "applicable": False,
                "reason": "Periodical issue",
                "metadata_patch": {
                    "isbn": ["9780140328721"],
                    "numberOfPages": 4,
                    "publisher": {"@type": "Organization", "name": "Press"},
                },
            }
        ),
        doc=doc,
        config={"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}},
    )

    assert result.metadata_patch is not None
    assert result.metadata_patch.publisher is not None
    assert result.metadata_patch.isbn is None
    assert result.metadata_patch.numberOfPages is None


def test_merged_metadata_error_lists_stable_code_and_path(evaluation_document) -> None:
    doc = replace(
        evaluation_document(),
        schema_org={
            "@context": "https://schema.org",
            "@type": "Newspaper",
            "numberOfPages": 4,
        },
    )

    with pytest.raises(
        GeminiModelResponseError,
        match=r"incompatible_property@\$\.numberOfPages",
    ):
        _parse_evaluation_response(
            json.dumps({"applicable": False, "reason": "Periodical issue"}),
            doc=doc,
            config={"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}},
        )
