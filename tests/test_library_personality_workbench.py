from __future__ import annotations

import pytest

from app.modules.library import personality_workbench


class _Db:
    def list_normalization_canonicals(self, entity_type):
        assert entity_type == "personality"
        return [
            {"canonical_id": 1, "display_name": "Тукай Габдулла", "status": "active", "identity_key": "тукай"},
            {"canonical_id": 2, "display_name": "Pending", "status": "active", "identity_key": None},
        ]

    def list_normalization_aliases(self, entity_type):
        assert entity_type == "personality"
        return [
            {"raw_name": "Габдулла Тукай", "canonical_id": 1, "decision_status": "linked", "successful_model": "gemini", "docs_count": 3},
            {"raw_name": "Unresolved", "canonical_id": 2, "decision_status": "linked", "docs_count": 99},
        ]


def test_personality_projection_contains_only_successful_canonicals_and_alias_counts() -> None:
    payload = personality_workbench.get_personalities(_Db())

    assert payload["personality_count"] == 1
    assert payload["items"] == [{
        "key": "canonical:1", "canonical_id": 1, "display_name": "Тукай Габдулла",
        "aliases": ["Габдулла Тукай"], "document_count": 3,
        "components": {"surname_full": None, "surname_initial": None, "name_full": None,
                       "name_initial": None, "father_name_full": None,
                       "father_name_initial": None, "title": None, "sex": None},
        "is_new": False,
    }]


def test_personality_changes_reject_raw_members_and_snapshot_conflicts() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        personality_workbench.validate_change_set({"keeps": ["raw"]})
    with pytest.raises(ValueError, match="two distinct"):
        personality_workbench.validate_change_set({"merges": [{"canonical_ids": [1], "display_name": "One"}]})

    with pytest.raises(ValueError, match="snapshot conflict"):
        personality_workbench.apply_personalities(_Db(), {"snapshot_token": "stale"})


def test_personality_change_set_accepts_strict_structured_correction() -> None:
    payload = personality_workbench.validate_change_set({
        "snapshot_token": "snapshot",
        "corrections": [{
            "canonical_id": 1,
            "components": {
                "surname_full": "Тукай",
                "name_full": "Габдулла",
                "father_name_full": "Мөхәммәтгариф",
                "sex": "M",
            },
        }],
    })

    correction = payload["corrections"][0]
    assert correction["display_name"] == "Тукай Габдулла Мөхәммәтгариф улы"
    assert correction["identity_key"]
    with pytest.raises(ValueError, match="components"):
        personality_workbench.validate_change_set({
            "corrections": [{"canonical_id": 1, "components": {"unknown": "x"}}]
        })
