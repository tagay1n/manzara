"""Api library collections coverage."""

from __future__ import annotations


def test_library_collections_overview_endpoint(
    test_client, override_operations
) -> None:
    client, _main_app = test_client

    override_operations(
        "payload",
        get_collection_overview=lambda: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "stats": {
                "total_collections": 12,
                "approved_collections": 5,
                "included_collections": 4,
                "suggested_collections": 6,
                "items_linked": 190,
            },
            "top_collections": [
                {
                    "collection_id": 101,
                    "title": "Шура журналы",
                    "item_count": 40,
                    "status": "approved",
                    "include_in_library": True,
                }
            ],
        },
    )

    response = client.get("/api/library/collections")
    assert response.status_code == 200
    payload = response.json()
    assert payload["overview"]["available"] is True
    assert payload["overview"]["stats"]["total_collections"] == 12
    assert payload["overview"]["top_collections"][0]["collection_id"] == 101


def test_library_collections_table_endpoint(test_client, override_operations) -> None:
    client, _main_app = test_client

    override_operations(
        "entities",
        list_library_collections=lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "page": 2,
            "page_size": 20,
            "total": 31,
            "total_pages": 2,
            "items": [
                {
                    "collection_id": 7,
                    "title": "Казан утлары",
                    "normalized_title": "казан утлары",
                    "status": "suggested",
                    "include_in_library": True,
                    "confidence": 0.88,
                    "item_count": 24,
                    "last_detected_at": "2026-03-25T10:00:00+00:00",
                }
            ],
        },
    )

    response = client.get("/api/library/collections/table?page=2&page_size=20")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["page"] == 2
    assert payload["items"][0]["collection_id"] == 7


def test_library_collection_items_endpoint(test_client, override_operations) -> None:
    client, _main_app = test_client

    override_operations(
        "entities",
        list_collection_items=lambda collection_id, **_kwargs: {
            "available": True,
            "error": None,
            "collection_id": collection_id,
            "items": [
                {
                    "md5": "abc123",
                    "item_title": "Казан утлары №1 (1999)",
                    "ya_path": "/library/kazan-utlary/1999-01.pdf",
                    "lib": False,
                }
            ],
        },
    )

    response = client.get("/api/library/collections/9/items")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["collection_id"] == 9
    assert payload["items"][0]["md5"] == "abc123"


def test_library_collection_review_endpoint(test_client, override_operations) -> None:
    client, _main_app = test_client

    override_operations(
        "entities",
        get_collection_review=lambda collection_id, **_kwargs: {
            "available": True,
            "error": None,
            "collection_id": collection_id,
            "summary": {"item_count": 24, "outliers": 2},
            "samples": [],
            "outliers": [],
        },
    )

    response = client.get("/api/library/collections/9/review")

    assert response.status_code == 200
    payload = response.json()
    assert payload["collection_id"] == 9
    assert payload["summary"]["item_count"] == 24


def test_library_collection_proposals_endpoint(
    test_client, override_operations
) -> None:
    client, _main_app = test_client
    override_operations(
        "entities",
        list_collection_proposals=lambda **kwargs: {
            "available": True,
            "page": kwargs["page"],
            "total_pages": 1,
            "total": 1,
            "items": [{"proposal_id": 17, "status": kwargs["status"]}],
        },
    )

    response = client.get(
        "/api/library/collection-proposals?status=review_ready&page=1"
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["proposal_id"] == 17


def test_library_collection_proposal_decision_endpoint(
    test_client, override_operations
) -> None:
    client, _main_app = test_client
    override_operations(
        "entities",
        decide_collection_proposal=lambda _db, proposal_id, decision, selected_md5s: {
            "ok": True,
            "proposal_id": proposal_id,
            "decision": decision,
            "selected_count": len(selected_md5s),
        },
    )

    response = client.post(
        "/api/library/collection-proposals/17/decision",
        json={"decision": "approve", "selected_md5s": ["a" * 32, "b" * 32]},
    )

    assert response.status_code == 200
    assert response.json()["selected_count"] == 2


def test_library_collection_update_endpoint(test_client, override_operations) -> None:
    client, _main_app = test_client

    override_operations(
        "entities",
        update_collection=lambda _db, collection_id, updates: {
            "ok": True,
            "collection": {
                "collection_id": collection_id,
                "status": updates.get("status", "suggested"),
                "include_in_library": bool(updates.get("include_in_library", False)),
                "title": str(updates.get("title") or "Collection"),
                "notes": str(updates.get("notes") or ""),
            },
            "updated_fields": sorted(list(updates.keys())),
        },
    )

    response = client.patch(
        "/api/library/collections/12",
        json={
            "status": "approved",
            "include_in_library": True,
            "title": "Шура журналы",
            "notes": "Manual review approved",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["collection"]["collection_id"] == 12
    assert payload["collection"]["status"] == "approved"
    assert payload["collection"]["include_in_library"] is True


def test_library_collection_merge_endpoint(test_client, override_operations) -> None:
    client, _main_app = test_client

    override_operations(
        "entities",
        merge_collections=lambda _db, source_collection_id, target_collection_id: {
            "ok": True,
            "source_collection_id": source_collection_id,
            "target_collection_id": target_collection_id,
            "moved_items": 6,
        },
    )

    response = client.post(
        "/api/library/collections/41/merge",
        json={"target_collection_id": 38},
    )

    assert response.status_code == 200
    assert response.json()["source_collection_id"] == 41
    assert response.json()["target_collection_id"] == 38
