from __future__ import annotations

import pytest

from app.modules.library import normalization


def test_create_canonical_group_deduplicates_and_retains_raw_aliases(monkeypatch) -> None:
    monkeypatch.setattr(
        normalization,
        "_runtime_snapshot_for_alias",
        lambda _entity_type, raw_name: {
            "raw_name": raw_name,
            "normalized_name": raw_name.casefold(),
            "script_label": "latin",
            "docs_count": 1,
            "mentions_count": 1,
            "marker_count": 0,
        },
    )

    class Db:
        def create_normalization_group(
            self, entity_type, display_name, normalized_name, snapshots, *, suggestion_ids
        ):
            return {
                "entity_type": entity_type,
                "display_name": display_name,
                "normalized_name": normalized_name,
                "aliases": [item["raw_name"] for item in snapshots],
                "suggestion_ids": suggestion_ids,
            }

    result = normalization.create_canonical_group(
        Db(),
        "publisher",
        display_name="  Canonical Publisher  ",
        raw_names=["Alias One", "Alias One", "Alias Two"],
        suggestion_ids=[3],
    )

    assert result["display_name"] == "Canonical Publisher"
    assert result["aliases"] == ["Alias One", "Alias Two"]
    assert result["suggestion_ids"] == [3]


def test_create_canonical_group_requires_at_least_one_alias() -> None:
    with pytest.raises(ValueError, match="raw_names must be non-empty"):
        normalization.create_canonical_group(
            object(), "publisher", display_name="Publisher", raw_names=[]
        )


def test_undo_refuses_non_reversible_refresh_event() -> None:
    class Db:
        def get_normalization_event(self, _event_id):
            return {
                "event_id": 1,
                "entity_type": "publisher",
                "action": "refresh_suggestions",
                "payload": {},
                "reverted": False,
            }

    with pytest.raises(ValueError, match="cannot be undone"):
        normalization.undo_event(Db(), "publisher", event_id=1)


def test_publisher_group_round_trip_preserves_aliases_and_rename(
    test_client, monkeypatch
) -> None:
    _client, main_app = test_client
    monkeypatch.setattr(
        normalization,
        "_runtime_snapshot_for_alias",
        lambda _entity_type, raw_name: {
            "raw_name": raw_name,
            "normalized_name": raw_name.casefold(),
            "script_label": "latin",
            "docs_count": 2,
            "mentions_count": 3,
            "marker_count": 1,
        },
    )

    created = normalization.create_canonical_group(
        main_app.state.db,
        "publisher",
        display_name="Canonical Publisher",
        raw_names=["Alias One", "Alias Two"],
    )
    canonical_id = created["canonical"]["canonical_id"]

    aliases = normalization.list_canonical_aliases(
        main_app.state.db, "publisher", canonical_id=canonical_id
    )
    assert [item["raw_name"] for item in aliases["items"]] == [
        "Alias One",
        "Alias Two",
    ]

    renamed = normalization.rename_canonical(
        main_app.state.db,
        "publisher",
        canonical_id=canonical_id,
        display_name="Renamed Publisher",
    )
    assert renamed["canonical"]["display_name"] == "Renamed Publisher"
    assert len(
        main_app.state.db.list_normalization_aliases_for_canonical(
            "publisher", canonical_id
        )
    ) == 2

    with pytest.raises(ValueError, match="already linked"):
        normalization.create_canonical_group(
            main_app.state.db,
            "publisher",
            display_name="Conflicting Publisher",
            raw_names=["Alias One"],
        )
