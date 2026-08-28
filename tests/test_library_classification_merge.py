"""Tests for classification merge helpers."""

from __future__ import annotations

from app.modules.library.classification_insights import (
    _rewrite_schema_org_classification_terms,
)


def test_rewrite_schema_org_classification_terms_replaces_managed_terms() -> None:
    original = {
        "@type": "Book",
        "about": [
            {"@type": "DefinedTerm", "termCode": "891.7", "inDefinedTermSet": "DDC"},
            {"@type": "DefinedTerm", "termCode": "Language > Tatar", "inDefinedTermSet": "CategoryPath"},
            {"@type": "DefinedTerm", "termCode": "004", "inDefinedTermSet": "UDC"},
            {"name": "free-text term"},
            "raw",
        ],
    }

    updated, changed = _rewrite_schema_org_classification_terms(
        original,
        target_ddc="810",
        target_path_parts=["Language", "Tatar"],
    )

    assert changed is True
    assert isinstance(updated, dict)
    about = updated.get("about")
    assert isinstance(about, list)
    assert {"@type": "DefinedTerm", "termCode": "810", "inDefinedTermSet": "DDC"} in about
    assert {
        "@type": "DefinedTerm",
        "termCode": "Language > Tatar",
        "inDefinedTermSet": "CategoryPath",
    } in about
    assert {"@type": "DefinedTerm", "termCode": "004", "inDefinedTermSet": "UDC"} in about
    assert {"name": "free-text term"} in about
    assert "raw" in about
    assert {"@type": "DefinedTerm", "termCode": "891.7", "inDefinedTermSet": "DDC"} not in about


def test_rewrite_schema_org_classification_terms_ignores_non_object_schema() -> None:
    updated, changed = _rewrite_schema_org_classification_terms(
        "not-json-object",
        target_ddc="810",
        target_path_parts=["Language", "Tatar"],
    )
    assert changed is False
    assert updated == "not-json-object"
