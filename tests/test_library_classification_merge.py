"""Tests for classification merge helpers."""

from __future__ import annotations

import pytest

from app.modules.library.classification_insights import (
    _build_tree,
    _rewrite_schema_org_classification_terms,
)
from app.modules.library.classification_editor import (
    StaleTaxonomyError,
    _prepare_change_set,
    _taxonomy_revision,
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
    assert {
        "@type": "DefinedTerm",
        "termCode": "810",
        "inDefinedTermSet": {"@type": "DefinedTermSet", "name": "DDC"},
    } in about
    assert {
        "@type": "DefinedTerm",
        "name": "Language > Tatar",
        "inDefinedTermSet": {"@type": "DefinedTermSet", "name": "CategoryPath"},
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


def _rows() -> list[dict]:
    return [
        {"id": 1, "ddc": "800", "path_en": ["Literature", "Tatar"], "usage_count": 7},
        {"id": 2, "ddc": "810", "path_en": ["Literature", "Poetry"], "usage_count": 3},
    ]


def test_prepare_change_set_normalizes_path_edits_and_merge_impact() -> None:
    rows = _rows()
    plan = _prepare_change_set(
        rows,
        {
            "base_revision": _taxonomy_revision(rows),
            "changes": [{"classification_id": 1, "path": ["Arts", "Tatar literature"]}],
            "merges": [{"source_classification_id": 2, "target_classification_id": 1}],
        },
    )

    assert plan["changes"] == [
        {"classification_id": 1, "path": ["Arts", "Tatar literature"]}
    ]
    assert plan["summary"] == {
        "path_changes": 1,
        "classification_merges": 1,
        "moved_documents": 3,
        "affected_documents": 10,
        "schema_org_updates": 10,
    }
    assert len(plan["change_set_hash"]) == 64


def test_prepare_change_set_rejects_stale_or_colliding_taxonomy() -> None:
    rows = _rows()
    with pytest.raises(StaleTaxonomyError):
        _prepare_change_set(rows, {"base_revision": "stale"})

    with pytest.raises(ValueError, match="same DDC and path"):
        _prepare_change_set(
            [*rows, {"id": 3, "ddc": "800", "path_en": ["Arts", "Books"], "usage_count": 1}],
            {
                "base_revision": _taxonomy_revision(
                    [*rows, {"id": 3, "ddc": "800", "path_en": ["Arts", "Books"], "usage_count": 1}]
                ),
                "changes": [{"classification_id": 3, "path": ["Literature", "Tatar"]}],
            },
        )


def test_prepare_change_set_rejects_boolean_ids_and_invalid_paths() -> None:
    rows = _rows()
    revision = _taxonomy_revision(rows)
    with pytest.raises(ValueError, match="positive integer"):
        _prepare_change_set(
            rows,
            {"base_revision": revision, "changes": [{"classification_id": True, "path": ["A", "B"]}]},
        )
    with pytest.raises(ValueError, match="2 to 8"):
        _prepare_change_set(
            rows,
            {"base_revision": revision, "changes": [{"classification_id": 1, "path": []}]},
        )


def test_tree_includes_terminal_classifications_even_without_documents() -> None:
    tree = _build_tree(
        [{"id": 9, "ddc": "900", "path_en": ["History", "Tatarstan"], "usage_count": 0}]
    )

    assert tree[0]["path"] == ["History"]
    leaf = tree[0]["children"][0]
    assert leaf["direct_usage_count"] == 0
    assert leaf["classifications"] == [
        {"classification_id": 9, "ddc": "900", "usage_count": 0}
    ]


def test_prepare_change_set_drops_noops_and_rejects_editing_merge_source() -> None:
    rows = _rows()
    revision = _taxonomy_revision(rows)
    plan = _prepare_change_set(
        rows,
        {
            "base_revision": revision,
            "changes": [{"classification_id": 1, "path": ["Literature", "Tatar"]}],
        },
    )
    assert plan["changes"] == []

    with pytest.raises(ValueError, match="merge sources"):
        _prepare_change_set(
            rows,
            {
                "base_revision": revision,
                "changes": [{"classification_id": 2, "path": ["Literature", "Verse"]}],
                "merges": [
                    {"source_classification_id": 2, "target_classification_id": 1}
                ],
            },
        )
