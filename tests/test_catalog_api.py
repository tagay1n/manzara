"""Admin transport keeps revisions, identity and private assets explicit."""

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.catalog_routes import register_catalog_routes


def client_for(repository, *, authenticate=lambda: "owner", private_url=None):
    app = FastAPI()
    register_catalog_routes(app, repository_provider=lambda: repository, actor_provider=authenticate,
                            preview_url_provider=private_url)
    return TestClient(app)


def test_admin_authentication_is_applied_to_reads_and_writes():
    def deny():
        raise HTTPException(status_code=401, detail="Authentication required")

    with client_for(object(), authenticate=deny) as client:
        assert client.get("/api/catalog/documents").status_code == 401
        assert client.patch("/api/catalog/publications/1", json={"revision": 1, "name": "A"}).status_code == 401


def test_private_storage_keys_are_not_returned_as_preview_urls():
    class Repository:
        def preview(self, _md5):
            return {"private": True, "request_id": 4, "source_page_count": 10,
                    "pages": [{"role": "first", "page_number": 1,
                               "small_key": "private/secret.webp", "large_key": "private/secret-large.webp"}]}

    with client_for(Repository()) as client:
        response = client.get("/api/catalog/documents/" + "a" * 32 + "/previews")
        assert response.status_code == 503
        assert "secret" not in response.text


def test_private_preview_delivery_is_delegated_to_authenticated_provider():
    class Repository:
        def preview(self, _md5):
            return {"private": True, "request_id": 4, "source_page_count": 10,
                    "pages": [{"role": "first", "page_number": 1,
                               "small_key": "private/a.webp", "large_key": "private/b.webp"}]}

    with client_for(Repository(), private_url=lambda **kwargs: "/authenticated/preview/4/" + kwargs["variant"]) as client:
        response = client.get("/api/catalog/documents/" + "a" * 32 + "/previews")
        assert response.status_code == 200
        assert response.json()["pages"][0]["small_url"] == "/authenticated/preview/4/small"
        assert "small_key" not in response.text


def test_conflicts_are_transport_conflicts():
    from app.catalog.contracts import CatalogConflict

    class Repository:
        def patch(self, *_args, **_kwargs):
            raise CatalogConflict("record changed")

    with client_for(Repository()) as client:
        response = client.patch("/api/catalog/publications/1", json={"revision": 1, "name": "A"})
        assert response.status_code == 409


def test_metadata_proposals_have_a_distinct_review_command():
    class Repository:
        def decide_metadata(self, proposal_id, **kwargs):
            assert proposal_id == 2 and kwargs["publication_revision"] == 5
            return {"status": "applied"}

    with client_for(Repository()) as client:
        response = client.post("/api/catalog/proposals/2/metadata-decision",
            json={"revision": 1, "publication_revision": 5, "decision": "apply"})
        assert response.status_code == 200
