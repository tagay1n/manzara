"""Catalog input and lossless bibliographic conversion contracts."""

import pytest

from app.catalog.contracts import CatalogConflict, parse_filters, validate_patch
from app.catalog.metadata import decompose_metadata, compose_metadata


def test_round_trip_preserves_partial_dates_roles_and_accessibility():
    source = {
        "@context": "https://schema.org", "@type": "Book", "name": "Example",
        "datePublished": "1930-05", "inLanguage": "tt-Cyrl,en",
        "author": [{"@type": "Person", "name": "A"}],
        "publisher": {"@type": "Organization", "name": "Press"},
        "contributor": [{"@type": "Role", "roleName": "Compiler",
                         "contributor": {"@type": "Person", "name": "B"}}],
        "isbn": ["978-0-123456-47-2"], "genre": ["Poetry"],
        "about": [{"@type": "DefinedTerm", "name": "History",
                   "inDefinedTermSet": {"@type": "DefinedTermSet", "name": "Subjects"}}],
        "audience": {"@type": "PeopleAudience", "suggestedMinAge": 12},
        "accessMode": ["textual", "visual"],
        "accessModeSufficient": [{"@type": "ItemList", "itemListElement": ["textual"]}],
        "isBasedOn": {"@type": "Book", "name": "Original", "inLanguage": "en",
                      "author": [{"@type": "Person", "name": "C"}],
                      "url": ["https://example.test/source"]},
    }
    assert compose_metadata(decompose_metadata(source)) == source


def test_unknown_metadata_is_retained_as_evidence_and_not_silently_dropped():
    with pytest.raises(ValueError, match="unsupported"):
        decompose_metadata({"@type": "Book", "unexpected": True})


@pytest.mark.parametrize("value", [True, 1.2, "2"])
def test_revision_requires_integral_json_number(value):
    with pytest.raises(ValueError, match="revision"):
        validate_patch("publication", {"revision": value, "name": "A"})


def test_filters_allow_registered_columns_without_sql_fragments():
    assert parse_filters("documents", [{"field": "title", "pattern": "^A", "ignore_case": True}])
    with pytest.raises(ValueError, match="field"):
        parse_filters("documents", [{"field": "title); DROP TABLE document", "pattern": "A"}])
    with pytest.raises(ValueError, match="boolean"):
        parse_filters("documents", [{"field": "title", "pattern": "A", "ignore_case": "false"}])


def test_conflict_is_distinct_from_validation_error():
    assert not issubclass(CatalogConflict, ValueError)
def test_every_catalog_relation_has_a_primary_key():
    from app.catalog.schema import build_metadata

    for table in build_metadata("test_catalog").tables.values():
        assert list(table.primary_key.columns), table.name


@pytest.mark.parametrize("field", ["kind", "approval", "work_type", "inclusion"])
def test_nested_values_in_enum_fields_are_rejected(field):
    kind = "entity" if field in {"kind", "approval"} else "publication"
    with pytest.raises(ValueError):
        validate_patch(kind, {"revision": 1, field: []})


def test_search_column_must_be_a_string():
    with pytest.raises(ValueError):
        parse_filters("documents", [{"field": [], "pattern": "x"}])
