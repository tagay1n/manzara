from __future__ import annotations

import pytest

from app.modules.library import personality_workbench


class _Db:
    def get_personality_decision_counts(self):
        return {}

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


def test_personality_repository_applies_structured_correction_and_retains_old_name(
    test_client,
) -> None:
    _client, main_app = test_client
    db = main_app.state.db
    canonical = db.persist_personality_normalization(
        raw_name="Г. Тукай",
        source_fingerprint="source-v1",
        document_count=2,
        mention_count=3,
        source_roles=["author"],
        components={
            "surname_full": "Тукай",
            "surname_initials": None,
            "name_full": None,
            "name_initials": "Г.",
            "father_name_full": None,
            "father_name_initials": None,
            "title": None,
            "sex": "M",
        },
        display_name="Тукай Г.",
        identity_key="тукай г.",
        model="gemini-test",
        prompt_version="prompt-v1",
        schema_version="schema-v1",
    )
    canonical_id = int(canonical["canonical_id"])
    change_set = personality_workbench.validate_change_set(
        {
            "corrections": [
                {
                    "canonical_id": canonical_id,
                    "components": {
                        "surname_full": "Тукай",
                        "name_full": "Габдулла",
                        "father_name_full": "Мөхәммәтгариф",
                        "sex": "M",
                    },
                }
            ]
        }
    )

    result = db.apply_personality_change_set(change_set)

    updated = db.get_normalization_canonical(canonical_id)
    assert result["touched_canonical_ids"] == [canonical_id]
    assert updated["display_name"] == "Тукай Габдулла Мөхәммәтгариф улы"
    assert updated["surname_full"] == "Тукай"
    assert updated["name_full"] == "Габдулла"
    assert updated["father_name_full"] == "Мөхәммәтгариф"
    assert updated["identity_key"] == change_set["corrections"][0]["identity_key"]
    assert db.get_normalization_alias("personality", "Тукай Г.")["canonical_id"] == canonical_id


def test_negative_decision_repository_review_and_explicit_retry(test_client):
    from app.modules.library.runtime.run_normalize_personalities import (
        PERSONALITY_NORMALIZATION_PROMPT_VERSION, SCHEMA_VERSION,
    )
    client, main_app = test_client
    db = main_app.state.db
    db.save_personality_checkpoint(raw_name="Example Press", source_fingerprint="source",
        document_count=2, mention_count=3, source_roles=["author"],
        prompt_version=PERSONALITY_NORMALIZATION_PROMPT_VERSION, schema_version=SCHEMA_VERSION,
        state="not_person", failure_context="An organization", retryable=False, completed=True,
        attempted_models={"first": {"kind": "decision", "outcome": "not_person", "reason": "An organization"}})
    db.save_personality_checkpoint(raw_name="Broken name", source_fingerprint="source",
        document_count=1, mention_count=1, source_roles=["editor"],
        prompt_version=PERSONALITY_NORMALIZATION_PROMPT_VERSION, schema_version=SCHEMA_VERSION,
        state="unusable", failure_context="Corrupted", retryable=False, completed=True)
    checkpoint = db.get_personality_checkpoint("Example Press")
    assert checkpoint["completed_at"]
    assert checkpoint["canonical_id"] is None
    projection = client.get("/api/library/personalities").json()
    assert projection["decision_counts"]["not_person"] == 1
    assert projection["decision_counts"]["unusable"] == 1
    review = client.get("/api/library/personalities/decisions?state=not_person&page=1").json()
    assert len(review["items"]) == 1
    assert review["items"][0]["reason"] == "An organization"
    retry = {"raw_name": "Example Press", "updated_at": checkpoint["updated_at"]}
    assert client.post("/api/library/personalities/decisions/retry", json=retry).status_code == 200
    revised = db.get_personality_checkpoint("Example Press")
    assert revised["state"] == "retry_requested"
    assert revised["retryable"] is True
    assert revised["failure_context"] == "An organization"
    assert revised["attempted_models"] == checkpoint["attempted_models"]
    assert client.post("/api/library/personalities/decisions/retry", json=retry).status_code == 409
    assert client.post("/api/library/personalities/decisions/retry", json={**retry, "unknown": True}).status_code == 400
    assert client.get("/api/library/personalities/decisions?state=invalid").status_code == 400
    assert client.get("/api/library/personalities/decisions?page=1.5").status_code == 422
