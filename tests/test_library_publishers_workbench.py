from __future__ import annotations

import pytest

from app.modules.library import publisher_workbench


class _Db:
    def list_normalization_canonicals(self, entity_type):
        assert entity_type == "publisher"
        return [
            {"canonical_id": 7, "display_name": "Tatar Books", "status": "active"},
            {"canonical_id": 8, "display_name": "Old Press", "status": "active"},
        ]

    def list_normalization_aliases(self, entity_type):
        assert entity_type == "publisher"
        return [
            {"raw_name": "Tatar Books", "canonical_id": 7, "decision_status": "linked"},
            {"raw_name": "Tat Books", "canonical_id": 7, "decision_status": "linked"},
            {"raw_name": "Old Press", "canonical_id": 8, "decision_status": "linked"},
        ]


def test_publisher_projection_combines_canonicals_and_unresolved_raw_names(monkeypatch) -> None:
    monkeypatch.setattr(
        publisher_workbench,
        "_query_aggregated_mentions",
        lambda _entity_type, **_kwargs: (
            [
                {"raw_name": "Tat Books", "docs_count": 3},
                {"raw_name": "New House", "docs_count": 2},
                {"raw_name": "Tatar Books", "docs_count": 4},
            ],
            "test",
        ),
    )

    payload = publisher_workbench.get_publishers(_Db())

    assert payload["new_count"] == 1
    assert [item["display_name"] for item in payload["items"]] == [
        "New House",
        "Old Press",
        "Tatar Books",
    ]
    canonical = payload["items"][-1]
    assert canonical["aliases"] == ["Tat Books", "Tatar Books"]
    assert canonical["document_count"] == 7
    assert canonical["is_new"] is False
    assert payload["items"][0]["raw_name"] == "New House"
    assert payload["items"][0]["is_new"] is True


def test_publisher_change_set_validation_rejects_boolean_ids_and_duplicate_members() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        publisher_workbench.validate_change_set(
            {"renames": [{"canonical_id": True, "display_name": "Name"}]}
        )
    with pytest.raises(ValueError, match="multiple merge groups"):
        publisher_workbench.validate_change_set(
            {
                "merges": [
                    {"canonical_ids": [7], "raw_names": ["One"], "display_name": "One"},
                    {"canonical_ids": [7], "raw_names": ["Two"], "display_name": "Two"},
                ]
            }
        )
