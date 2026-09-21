"""Api library entities coverage."""

from __future__ import annotations


def test_library_personalities_overview_endpoint(
    test_client, override_operations
) -> None:
    client, _main_app = test_client

    override_operations(
        "payload",
        get_personality_overview=lambda: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "stats": {
                "total_mentions": 120,
                "docs_with_authors": 80,
                "unique_raw_names": 40,
                "unique_normalized_names": 30,
                "mixed_script_mentions": 5,
                "patronymic_mentions": 12,
            },
            "top_personalities": [{"raw_name": "Габдулла Тукай", "docs_count": 9}],
        },
    )

    response = client.get("/api/library/personalities")
    assert response.status_code == 200
    payload = response.json()
    assert payload["overview"]["available"] is True
    assert payload["overview"]["stats"]["total_mentions"] == 120
    assert payload["overview"]["top_personalities"][0]["raw_name"] == "Габдулла Тукай"


def test_library_personalities_table_endpoint(test_client, override_operations) -> None:
    client, _main_app = test_client

    override_operations(
        "entities",
        list_personalities=lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "page": 1,
            "page_size": 25,
            "total": 1,
            "total_pages": 1,
            "items": [
                {
                    "raw_name": "Габдулла Тукай",
                    "normalized_name": "габдулла тукай",
                    "script_label": "cyrillic",
                    "docs_count": 9,
                    "mentions_count": 10,
                    "patronymic_mentions": 0,
                }
            ],
        },
    )

    response = client.get("/api/library/personalities/table?page=1&page_size=25")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["items"][0]["raw_name"] == "Габдулла Тукай"


def test_library_personalities_insights_endpoint(
    test_client, override_operations
) -> None:
    client, _main_app = test_client

    override_operations(
        "entities",
        get_personality_insights=lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "script_distribution": [
                {"script_label": "cyrillic", "mentions_count": 10, "share_pct": 100.0}
            ],
            "variant_clusters": [
                {"normalized_name": "габдулла тукай", "variants_count": 2}
            ],
            "ambiguous_queue": {"total": 1, "items": [{"raw_name": "Тукай"}]},
        },
    )

    response = client.get("/api/library/personalities/insights")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["script_distribution"][0]["script_label"] == "cyrillic"
    assert payload["ambiguous_queue"]["total"] == 1


def test_library_publishers_overview_endpoint(test_client, override_operations) -> None:
    client, _main_app = test_client

    override_operations(
        "payload",
        get_publisher_overview=lambda: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "stats": {
                "total_mentions": 90,
                "docs_with_publishers": 70,
                "unique_raw_names": 35,
                "unique_normalized_names": 28,
                "mixed_script_mentions": 4,
                "org_marker_mentions": 17,
            },
            "top_publishers": [{"raw_name": "Таткнигоиздат", "docs_count": 11}],
        },
    )

    response = client.get("/api/library/publishers")
    assert response.status_code == 200
    payload = response.json()
    assert payload["overview"]["available"] is True
    assert payload["overview"]["stats"]["docs_with_publishers"] == 70
    assert payload["overview"]["top_publishers"][0]["raw_name"] == "Таткнигоиздат"


def test_library_publisher_documents_endpoint(test_client, override_operations) -> None:
    client, _main_app = test_client

    override_operations(
        "normalization",
        list_publisher_documents=lambda _db, publisher_key, *, page: {
            "available": True,
            "publisher_key": publisher_key,
            "page": page,
            "page_size": 10,
            "total": 11,
            "has_more": True,
            "items": [
                {
                    "md5": "a" * 32,
                    "label": "/books/tatar-book.pdf",
                }
            ],
        },
    )

    response = client.get("/api/library/publishers/documents?publisher_key=canonical%3A7&page=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["publisher_key"] == "canonical:7"
    assert payload["page"] == 2
    assert payload["page_size"] == 10
    assert payload["items"][0]["md5"] == "a" * 32


def test_library_publishers_table_endpoint(test_client, override_operations) -> None:
    client, _main_app = test_client

    override_operations(
        "entities",
        list_publishers=lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "page": 1,
            "page_size": 25,
            "total": 1,
            "total_pages": 1,
            "items": [
                {
                    "raw_name": "Таткнигоиздат",
                    "normalized_name": "таткнигоиздат",
                    "script_label": "cyrillic",
                    "docs_count": 11,
                    "mentions_count": 12,
                    "org_marker_mentions": 0,
                }
            ],
        },
    )

    response = client.get("/api/library/publishers/table?page=1&page_size=25")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["items"][0]["raw_name"] == "Таткнигоиздат"


def test_library_publishers_insights_endpoint(test_client, override_operations) -> None:
    client, _main_app = test_client

    override_operations(
        "entities",
        get_publisher_insights=lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "script_distribution": [
                {"script_label": "cyrillic", "mentions_count": 15, "share_pct": 100.0}
            ],
            "variant_clusters": [
                {"normalized_name": "таткнигоиздат", "variants_count": 2}
            ],
            "ambiguous_queue": {"total": 1, "items": [{"raw_name": "Татиздат"}]},
        },
    )

    response = client.get("/api/library/publishers/insights")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["script_distribution"][0]["script_label"] == "cyrillic"
    assert payload["ambiguous_queue"]["total"] == 1
