"""Api library classification coverage."""

from __future__ import annotations


def test_library_classifications_table_endpoint(
    test_client, override_operations
) -> None:
    client, _main_app = test_client

    override_operations(
        "classification",
        list_classifications=lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "page": 2,
            "page_size": 10,
            "total": 23,
            "total_pages": 3,
            "items": [
                {
                    "classification_id": 7,
                    "ddc": "891.7",
                    "path": "Language / Tatar",
                    "status": "approved",
                    "created_by": "gemini",
                    "created_at": "2026-03-01T10:00:00",
                    "usage_count": 12,
                }
            ],
        },
    )

    response = client.get("/api/library/classifications?page=2&page_size=10")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["page"] == 2
    assert payload["items"][0]["classification_id"] == 7


def test_library_classification_insights_endpoint(
    test_client, override_operations
) -> None:
    client, _main_app = test_client

    override_operations(
        "classification",
        get_classification_insights=lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "revision": "abc",
            "tree": [
                {
                    "name": "Language",
                    "path": ["Language"],
                    "usage_count": 10,
                    "children": [],
                }
            ],
            "distribution": [{"bucket": "800", "usage_count": 10, "share_pct": 100.0}],
        },
    )

    response = client.get("/api/library/classifications/insights")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["distribution"][0]["bucket"] == "800"
    assert payload["revision"] == "abc"


def test_library_classification_documents_endpoint(
    test_client,
    override_operations,
) -> None:
    client, _main_app = test_client
    override_operations(
        "classification",
        list_classification_documents=lambda ids, **_kwargs: {
            "available": True,
            "classification_ids": ids,
            "total": 1,
            "has_more": False,
            "items": [{"md5": "a" * 32, "title": "Book"}],
        },
    )
    response = client.get(
        "/api/library/classifications/documents?classification_ids=2,3&limit=10"
    )
    assert response.status_code == 200
    assert response.json()["classification_ids"] == [2, 3]


def test_library_classification_change_set_preview_and_apply_endpoints(
    test_client,
    override_operations,
) -> None:
    client, _main_app = test_client
    captured: list[tuple[str, dict]] = []
    override_operations(
        "classification",
        preview_change_set=lambda payload: (
            captured.append(("preview", payload))
            or {"available": True, "change_set_hash": "hash"}
        ),
    )
    override_operations(
        "classification",
        apply_change_set=lambda payload: (
            captured.append(("apply", payload)) or {"available": True, "applied": True}
        ),
    )
    body = {"base_revision": "rev", "changes": [], "merges": []}
    assert (
        client.post(
            "/api/library/classifications/change-set/preview", json=body
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/library/classifications/change-set/apply",
            json={**body, "confirmed": True, "change_set_hash": "hash"},
        ).status_code
        == 200
    )
    assert [item[0] for item in captured] == ["preview", "apply"]


def test_removed_classification_workflow_endpoints_are_absent(test_client) -> None:
    client, _main_app = test_client
    assert client.get(
        "/api/library/classifications/normalization-preview"
    ).status_code in {404, 422}
    assert client.get("/api/library/classifications/merge-candidates").status_code in {
        404,
        422,
    }
    assert client.post("/api/library/classifications/merge", json={}).status_code in {
        404,
        405,
    }


def test_library_classification_detail_endpoint(
    test_client, override_operations
) -> None:
    client, _main_app = test_client

    override_operations(
        "payload",
        get_classification_detail=lambda classification_id, docs_page, docs_page_size: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "classification": {
                "classification_id": classification_id,
                "ddc": "891.7",
                "path": "Language / Tatar",
                "path_tt": "Тел / Татар",
                "status": "approved",
                "created_by": "gemini",
                "created_at": "2026-03-01T10:00:00",
                "usage_count": 9,
            },
            "linked_docs": {
                "page": docs_page,
                "page_size": docs_page_size,
                "total": 1,
                "total_pages": 1,
                "items": [{"md5": "abc"}],
            },
            "language_distribution": [{"language": "tt-Cyrl", "count": 1}],
        },
    )

    response = client.get(
        "/api/library/classifications/7?docs_page=1&docs_page_size=20"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["detail"]["available"] is True
    assert payload["detail"]["classification"]["classification_id"] == 7
    assert payload["detail"]["linked_docs"]["items"][0]["md5"] == "abc"
