"""Api library documents coverage."""

from __future__ import annotations


def test_library_document_open_redirects_to_resolved_storage(
    test_client, monkeypatch
) -> None:
    client, _main_app = test_client
    from app import library_document_routes

    monkeypatch.setattr(
        library_document_routes,
        "resolve_document_open_url",
        lambda _state, md5: f"https://objects.example.test/{md5}.pdf",
    )

    digest = "a" * 32
    response = client.get(
        f"/api/library/documents/{digest}/open",
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == f"https://objects.example.test/{digest}.pdf"
