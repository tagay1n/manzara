"""Api library normalization coverage."""

from __future__ import annotations


def test_library_normalization_overview_endpoint(
    test_client, override_operations
) -> None:
    client, _main_app = test_client

    override_operations(
        "payload",
        get_normalization_dashboard=lambda _db, _entity_type: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "stats": {
                "total_aliases": 30,
                "docs_with_entities": 20,
                "canonicals": 9,
                "linked": 7,
                "unreviewed": 12,
                "suggested": 6,
                "coverage_pct": 40.0,
            },
            "suggestions": {"open_total": 6, "high": 2, "medium": 3, "low": 1},
            "top_unresolved": [{"raw_name": "Тукай", "docs_count": 5}],
        },
    )
    override_operations(
        "payload",
        get_normalization_quality=lambda _db, _entity_type: {
            "available": True,
            "error": None,
            "stats": {"coverage_pct": 40.0},
        },
    )
    override_operations(
        "payload",
        list_suggestions=lambda _db, _entity_type, limit=80: {
            "available": True,
            "error": None,
            "items": [{"raw_name": "Тукай"}][:limit],
        },
    )
    override_operations(
        "payload",
        list_normalization_history=lambda _db, _entity_type, limit=20: {
            "available": True,
            "error": None,
            "items": [{"event_id": 1}][:limit],
        },
    )

    response = client.get("/api/library/normalization/personality")
    assert response.status_code == 200
    payload = response.json()
    assert payload["entity_type"] == "personality"
    assert payload["dashboard"]["available"] is True
    assert payload["dashboard"]["stats"]["total_aliases"] == 30
    assert payload["quality"]["available"] is True
    assert payload["suggestions"]["items"][0]["raw_name"] == "Тукай"
    assert payload["history_preview"]["items"][0]["event_id"] == 1


def test_library_normalization_queue_endpoint(test_client, override_operations) -> None:
    client, _main_app = test_client

    override_operations(
        "normalization",
        get_review_queue=lambda _db, _entity_type, **_kwargs: {
            "available": True,
            "error": None,
            "page": 1,
            "page_size": 40,
            "total": 1,
            "total_pages": 1,
            "items": [
                {
                    "raw_name": "Тукай",
                    "normalized_name": "тукай",
                    "script_label": "cyrillic",
                    "docs_count": 4,
                    "mentions_count": 5,
                    "queue_status": "unreviewed",
                }
            ],
        },
    )

    response = client.get(
        "/api/library/normalization/personality/queue?status=all&page=1&page_size=40"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["items"][0]["raw_name"] == "Тукай"
    assert payload["items"][0]["queue_status"] == "unreviewed"


def test_library_normalization_link_decision_endpoint(
    test_client, override_operations
) -> None:
    client, _main_app = test_client

    override_operations(
        "normalization",
        link_alias=lambda _db, _entity_type, **kwargs: {
            "alias": {
                "raw_name": kwargs["raw_name"],
                "canonical_id": kwargs["canonical_id"],
                "decision_status": "linked",
            },
            "event": {"event_id": 4},
        },
    )

    response = client.post(
        "/api/library/normalization/personality/decisions/link",
        json={"raw_name": "Тукай", "canonical_id": 9, "suggestion_ids": [3]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["alias"]["raw_name"] == "Тукай"
    assert payload["alias"]["canonical_id"] == 9
    assert payload["event"]["event_id"] == 4


def test_library_normalization_link_rejects_invalid_suggestion_ids(test_client) -> None:
    client, _main_app = test_client

    response = client.post(
        "/api/library/normalization/personality/decisions/link",
        json={"raw_name": "Тукай", "canonical_id": 9, "suggestion_ids": ["bad"]},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "suggestion_ids must be integers"


def test_library_normalization_reject_rejects_invalid_suggestion_ids(
    test_client,
) -> None:
    client, _main_app = test_client

    response = client.post(
        "/api/library/normalization/personality/decisions/reject",
        json={"raw_name": "Тукай", "suggestion_ids": ["bad"]},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "suggestion_ids must be integers"


def test_library_normalization_bulk_link_rejects_invalid_raw_names_shape(
    test_client,
) -> None:
    client, _main_app = test_client

    response = client.post(
        "/api/library/normalization/personality/bulk/link",
        json={"raw_names": "Alias One", "canonical_id": 9},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "raw_names must be a list of strings"


def test_library_normalization_bulk_reject_rejects_invalid_raw_names_shape(
    test_client,
) -> None:
    client, _main_app = test_client

    response = client.post(
        "/api/library/normalization/personality/bulk/reject",
        json={"raw_names": "Alias One"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "raw_names must be a list of strings"


def test_personality_normalization_refresh_suggestions_endpoint(
    test_client, override_operations
) -> None:
    client, _main_app = test_client

    override_operations(
        "normalization",
        refresh_suggestions=lambda _db, _entity_type, limit, use_gemini: {
            "generated": limit,
            "bands": {"high": 1, "medium": 2, "low": 3},
            "event": {"event_id": 9, "use_gemini": use_gemini},
        },
    )

    response = client.post(
        "/api/library/normalization/personality/suggestions/refresh",
        json={"limit": 77, "use_gemini": False},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["generated"] == 77
    assert payload["event"]["event_id"] == 9
    assert payload["event"]["use_gemini"] is False


def test_publisher_legacy_suggestion_routes_are_not_supported(test_client) -> None:
    client, _main_app = test_client
    response = client.post(
        "/api/library/normalization/publisher/suggestions/refresh",
        json={"limit": 7},
    )
    assert response.status_code == 404


def test_publisher_legacy_mutation_route_is_not_supported(test_client) -> None:
    client, _main_app = test_client
    response = client.patch(
        "/api/library/normalization/publisher/canonicals/17",
        json={"display_name": "Canonical Publisher"},
    )
    assert response.status_code == 404


def test_library_normalization_rejects_unknown_entity(test_client) -> None:
    client, _main_app = test_client

    response = client.get("/api/library/normalization/unknown")
    assert response.status_code == 404


def test_publisher_normalization_page_redirects_to_combined_page(test_client) -> None:
    client, _main_app = test_client
    response = client.get("/library/normalization/publisher", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/library/publishers"
