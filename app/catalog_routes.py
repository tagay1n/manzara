"""Authenticated catalog HTTP contracts reusable by a separate admin service."""

import json
import re
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.catalog.contracts import CatalogConflict, CatalogNotFound, integer, nonblank


def register_catalog_routes(
    app: FastAPI, *, repository_provider: Callable, actor_provider: Callable,
    preview_url_provider: Callable | None = None,
) -> None:
    """Authentication is mandatory and supplied by the hosting application's boundary."""
    router = APIRouter(prefix="/api/catalog", dependencies=[Depends(actor_provider)])

    @app.exception_handler(CatalogConflict)
    def conflict(_request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(CatalogNotFound)
    def missing(_request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    def call(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Catalog relationship or uniqueness conflict") from exc

    def filters(value):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="filters must be a JSON array") from exc

    def digest(value):
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise HTTPException(status_code=400, detail="md5 must be 32 lowercase hexadecimal characters")
        return value

    @router.get("/documents")
    def documents(inclusion: str = "all", filters_json: str = Query("[]", alias="filters"), page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), repo=Depends(repository_provider)):
        return call(repo.list_documents, inclusion=inclusion, filters=filters(filters_json), page=page, page_size=page_size)

    @router.get("/documents/{md5}")
    def document(md5: str, repo=Depends(repository_provider)):
        value = call(repo.get, "document", digest(md5))
        return {"document": value, "publication": call(repo.get, "publication", value["publication_id"]),
                "metadata": call(repo.metadata, md5), "schema_org": call(repo.schema_org, md5)}

    @router.patch("/documents/{md5}")
    def edit_document(md5: str, payload: dict[str, Any] = Body(...), actor=Depends(actor_provider), repo=Depends(repository_provider)):
        return call(repo.patch, "document", digest(md5), payload, actor=actor)

    @router.get("/documents/{md5}/evidence")
    def evidence(md5: str, after_id: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100), repo=Depends(repository_provider)):
        return call(repo.evidence, digest(md5), after_id=after_id, limit=limit)

    @router.put("/documents/{md5}/metadata")
    def edit_metadata(md5: str, payload: dict[str, Any] = Body(...), actor=Depends(actor_provider), repo=Depends(repository_provider)):
        if set(payload) != {"revision", "document_revision", "schema_org"}:
            raise HTTPException(status_code=400, detail="metadata requires publication revision, document_revision, and schema_org")
        return call(repo.apply_metadata, digest(md5), payload["schema_org"], actor=actor,
            revision=call(integer, payload["revision"], "revision"), document_revision=call(integer, payload["document_revision"], "document_revision"))

    @router.post("/documents/{md5}/previews/regenerate", status_code=202)
    def request_preview(md5: str, payload: dict[str, Any] = Body(...), actor=Depends(actor_provider), repo=Depends(repository_provider)):
        if set(payload) != {"idempotency_key"}:
            raise HTTPException(status_code=400, detail="preview request requires idempotency_key")
        row = call(repo.request_preview, digest(md5), actor=actor, idempotency_key=payload["idempotency_key"])
        return {key: row[key] for key in ("request_id", "status", "recipe")}

    @router.get("/documents/{md5}/previews")
    def previews(md5: str, actor=Depends(actor_provider), repo=Depends(repository_provider)):
        row = call(repo.preview, digest(md5))
        if row is None:
            return {"status": "pending", "pages": []}
        if row["pages"] and preview_url_provider is None:
            raise HTTPException(status_code=503, detail="Authenticated preview delivery is not configured")
        pages = []
        for page in row["pages"]:
            item = {"role": page["role"], "page_number": page["page_number"]}
            for variant in ("small", "large"):
                if page.get(variant + "_key") is not None:
                    item[variant + "_url"] = preview_url_provider(actor=actor, md5=md5,
                        request_id=row["request_id"], private=row["private"], key=page[variant + "_key"], variant=variant)
            pages.append(item)
        return {"status": "ready", "request_id": row["request_id"], "source_page_count": row["source_page_count"], "pages": pages}

    def register_records(resource, kind):
        def listing(filters_json: str = Query("[]", alias="filters"), page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), approval: str | None = None, role: str | None = None, repo=Depends(repository_provider)):
            return call(repo.list_records, kind, filters=filters(filters_json), page=page, page_size=page_size, approval=approval, role=role)

        def detail(record_id: int, repo=Depends(repository_provider)):
            return call(repo.get, kind, record_id)

        def edit(record_id: int, payload: dict[str, Any] = Body(...), actor=Depends(actor_provider), repo=Depends(repository_provider)):
            return call(repo.patch, kind, record_id, payload, actor=actor)

        router.add_api_route("/" + resource, listing, methods=["GET"], name="catalog_list_" + resource)
        router.add_api_route("/" + resource + "/{record_id}", detail, methods=["GET"], name="catalog_get_" + resource)
        router.add_api_route("/" + resource + "/{record_id}", edit, methods=["PATCH"], name="catalog_patch_" + resource)

    for resource, kind in (("publications", "publication"), ("entities", "entity"), ("collections", "collection")):
        register_records(resource, kind)

    @router.post("/entities", status_code=201)
    def create_entity(payload: dict[str, Any] = Body(...), actor=Depends(actor_provider), repo=Depends(repository_provider)):
        if set(payload) != {"kind", "display_name"}:
            raise HTTPException(status_code=400, detail="identity requires kind and display_name")
        return call(repo.create_entity, payload["kind"], payload["display_name"], actor=actor)

    @router.post("/collections", status_code=201)
    def create_collection(payload: dict[str, Any] = Body(...), actor=Depends(actor_provider), repo=Depends(repository_provider)):
        if set(payload) - {"title", "notes"} or "title" not in payload:
            raise HTTPException(status_code=400, detail="collection requires title and optional notes")
        return call(repo.create_collection, actor=actor, **payload)

    @router.get("/entities/{entity_id}/aliases")
    def aliases(entity_id: int, repo=Depends(repository_provider)):
        return call(repo.list_aliases, entity_id)

    @router.get("/names")
    def names(kind: str | None = None, role: str | None = None, unresolved: bool = False, filters_json: str = Query("[]", alias="filters"), page: int = Query(1, ge=1), repo=Depends(repository_provider)):
        return call(repo.list_names, kind=kind, role=role, unresolved=unresolved, filters=filters(filters_json), page=page)

    @router.post("/entities/{entity_id}/aliases", status_code=201)
    def add_alias(entity_id: int, payload: dict[str, Any] = Body(...), actor=Depends(actor_provider), repo=Depends(repository_provider)):
        if set(payload) != {"revision", "raw_name"}:
            raise HTTPException(status_code=400, detail="alias requires revision and raw_name")
        return call(repo.add_alias, entity_id, payload["raw_name"], revision=payload["revision"], actor=actor)

    @router.post("/contributions/{contribution_id}/resolve")
    def resolve(contribution_id: int, payload: dict[str, Any] = Body(...), actor=Depends(actor_provider), repo=Depends(repository_provider)):
        if set(payload) != {"entity_id", "revision"}:
            raise HTTPException(status_code=400, detail="resolution requires entity_id and revision")
        return call(repo.resolve_contribution, contribution_id, call(integer, payload["entity_id"], "entity_id"), revision=payload["revision"], actor=actor)

    @router.post("/aliases/{alias_id}/reassign")
    def reassign(alias_id: int, payload: dict[str, Any] = Body(...), actor=Depends(actor_provider), repo=Depends(repository_provider)):
        if set(payload) != {"entity_id", "revision", "contribution_revisions"} or not isinstance(payload["contribution_revisions"], dict):
            raise HTTPException(status_code=400, detail="alias reassignment requires entity_id, revision, and reviewed mentions")
        revisions = {}
        for key, value in payload["contribution_revisions"].items():
            if not isinstance(key, str) or not key.isdecimal():
                raise HTTPException(status_code=400, detail="mention keys must be positive integer strings")
            revisions[int(key)] = call(integer, value, "revision")
        return call(repo.reassign_alias, alias_id, call(integer, payload["entity_id"], "entity_id"), revision=payload["revision"], contribution_revisions=revisions, actor=actor)

    @router.delete("/aliases/{alias_id}")
    def remove_alias(alias_id: int, payload: dict[str, Any] = Body(...), actor=Depends(actor_provider), repo=Depends(repository_provider)):
        if set(payload) != {"revision"}:
            raise HTTPException(status_code=400, detail="alias removal requires revision")
        return call(repo.remove_alias, alias_id, revision=payload["revision"], actor=actor)

    @router.post("/publications/{publication_id}/group")
    def group(publication_id: int, payload: dict[str, Any] = Body(...), actor=Depends(actor_provider), repo=Depends(repository_provider)):
        if set(payload) != {"revision", "sources", "resolutions"} or not isinstance(payload["sources"], dict):
            raise HTTPException(status_code=400, detail="grouping requires revision, sources and resolutions")
        sources = {}
        for key, value in payload["sources"].items():
            if not isinstance(key, str) or not key.isdecimal():
                raise HTTPException(status_code=400, detail="source IDs must be positive integer strings")
            sources[int(key)] = call(integer, value, "revision")
        return call(repo.merge_publications, publication_id, sources, revision=payload["revision"], resolutions=payload["resolutions"], actor=actor)

    @router.get("/proposals")
    def proposals(kind: str | None = None, status: str = "pending", page: int = Query(1, ge=1), repo=Depends(repository_provider)):
        return call(repo.list_proposals, kind=kind, status=status, page=page)

    @router.get("/proposals/{proposal_id}")
    def proposal_detail(proposal_id: int, repo=Depends(repository_provider)):
        return call(repo.proposal_detail, proposal_id)

    @router.post("/proposals", status_code=201)
    def create_cluster(payload: dict[str, Any] = Body(...), actor=Depends(actor_provider), repo=Depends(repository_provider)):
        if set(payload) != {"members", "display_name", "evidence"} or not isinstance(payload["evidence"], dict):
            raise HTTPException(status_code=400, detail="cluster requires members, display_name, and evidence")
        return call(repo.create_cluster, actor=actor, **payload)

    @router.post("/proposals/{proposal_id}/metadata-decision")
    def metadata_decision(proposal_id: int, payload: dict[str, Any] = Body(...), actor=Depends(actor_provider), repo=Depends(repository_provider)):
        if set(payload) - {"revision", "publication_revision", "document_revision", "decision"} or not {"revision", "publication_revision", "decision"} <= payload.keys():
            raise HTTPException(status_code=400, detail="metadata review requires proposal and publication revisions and a decision")
        return call(repo.decide_metadata, proposal_id, actor=actor, **payload)

    @router.post("/proposals/{proposal_id}/decision")
    def decision(proposal_id: int, payload: dict[str, Any] = Body(...), actor=Depends(actor_provider), repo=Depends(repository_provider)):
        if set(payload) - {"revision", "decision", "display_name", "selected_members"} or not {"revision", "decision"} <= payload.keys():
            raise HTTPException(status_code=400, detail="unsupported proposal decision")
        return call(repo.decide_cluster, proposal_id, actor=actor, **payload)

    @router.get("/classification-nodes")
    def classification_nodes(filters_json: str = Query("[]", alias="filters"), page: int = Query(1, ge=1), repo=Depends(repository_provider)):
        return call(repo.list_classification_nodes, filters=filters(filters_json), page=page)

    @router.post("/classification-nodes", status_code=201)
    def create_classification(payload: dict[str, Any] = Body(...), actor=Depends(actor_provider), repo=Depends(repository_provider)):
        if set(payload) - {"ddc", "label_en", "label_tt", "parent_id"} or not {"ddc", "label_en"} <= payload.keys():
            raise HTTPException(status_code=400, detail="classification requires ddc and label_en")
        return call(repo.create_classification_node, actor=actor, **payload)

    @router.post("/publications/{publication_id}/classification")
    def assign_classification(publication_id: int, payload: dict[str, Any] = Body(...), actor=Depends(actor_provider), repo=Depends(repository_provider)):
        if set(payload) != {"revision", "node_id"}:
            raise HTTPException(status_code=400, detail="classification assignment requires revision and node_id")
        return call(repo.assign_classification, publication_id, actor=actor, **payload)

    @router.get("/classification-nodes/{node_id}/path")
    def classification_path(node_id: int, repo=Depends(repository_provider)):
        return call(repo.classification_path, node_id)

    @router.patch("/classification-nodes/{node_id}")
    def edit_classification(node_id: int, payload: dict[str, Any] = Body(...), actor=Depends(actor_provider), repo=Depends(repository_provider)):
        return call(repo.patch, "classification_node", node_id, payload, actor=actor)

    @router.get("/history/{kind}/{key}")
    def history(kind: str, key: str, repo=Depends(repository_provider)):
        return call(repo.revisions, nonblank(kind, "kind"), nonblank(key, "key"))

    app.include_router(router)
