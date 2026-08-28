"""API/runtime behavior tests for Manzara."""

from __future__ import annotations

import time



def _wait_for_status(main_app, run_id: int, expected: set[str], timeout_seconds: float = 4.0):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        run = main_app.state.db.get_run(run_id)
        if run and run["status"] in expected:
            return run
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not reach expected status: {expected}")


def test_library_classifications_table_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "list_classifications",
        lambda **_kwargs: {
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


def test_library_classification_insights_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_classification_insights",
        lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "tree": [{"name": "Language", "usage_count": 10, "children": []}],
            "distribution": [{"bucket": "800", "usage_count": 10, "share_pct": 100.0}],
            "duplicates": [],
            "unclassified_queue": {"total": 1, "items": [{"md5": "abc"}]},
        },
    )

    response = client.get("/api/library/classifications/insights")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["distribution"][0]["bucket"] == "800"
    assert payload["unclassified_queue"]["total"] == 1


def test_library_classification_normalization_preview_endpoint(
    test_client,
    monkeypatch,
) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_normalization_preview",
        lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "rules": {"drop_segments": ["turkic literature"]},
            "summary": {
                "total_rows_scanned": 100,
                "affected_classifications": 7,
                "estimated_reassigned_documents": 42,
                "merge_group_candidates": 3,
            },
            "affected_preview": [{"classification_id": 1}],
            "merge_groups": [{"normalized_path": "language / tatar"}],
        },
    )

    response = client.get(
        "/api/library/classifications/normalization-preview"
        "?drop_segments=Turkic%20literature,Tatar&limit=120&row_limit=5000"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["summary"]["affected_classifications"] == 7
    assert payload["rules"]["drop_segments"][0] == "turkic literature"


def test_library_classification_merge_candidates_endpoint(
    test_client,
    monkeypatch,
) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_merge_candidates",
        lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "summary": {
                "rows_scanned": 120,
                "candidate_count": 2,
                "min_score": 0.78,
            },
            "candidates": [
                {
                    "issue": "near_duplicate",
                    "score": 0.91,
                    "impact": 11,
                    "recommended_primary_classification_id": 3,
                    "primary": {"classification_id": 3, "ddc": "891.7", "path": "A", "usage_count": 7},
                    "secondary": {"classification_id": 4, "ddc": "891.7", "path": "B", "usage_count": 4},
                }
            ],
        },
    )

    response = client.get(
        "/api/library/classifications/merge-candidates"
        "?limit=80&min_score=0.8&row_limit=1000"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["summary"]["candidate_count"] == 2
    assert payload["candidates"][0]["recommended_primary_classification_id"] == 3


def test_library_classification_merge_endpoint(
    test_client,
    monkeypatch,
) -> None:
    client, main_app = test_client
    captured: dict[str, int | str] = {}

    def _fake_merge_classifications(*, source_classification_id, target_classification_id, reason):  # noqa: ANN001
        captured["source"] = int(source_classification_id)
        captured["target"] = int(target_classification_id)
        captured["reason"] = str(reason)
        return {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "source_classification_id": int(source_classification_id),
            "target_classification_id": int(target_classification_id),
            "moved_docs_count": 17,
            "schema_org_updated_count": 17,
            "source_deleted": True,
        }

    monkeypatch.setattr(main_app, "merge_classifications", _fake_merge_classifications)

    response = client.post(
        "/api/library/classifications/merge",
        json={
            "source_classification_id": 7,
            "target_classification_id": 3,
            "reason": "manual_merge",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["source_classification_id"] == 7
    assert payload["target_classification_id"] == 3
    assert payload["moved_docs_count"] == 17
    assert payload["source_deleted"] is True
    assert captured == {"source": 7, "target": 3, "reason": "manual_merge"}


def test_library_classification_merge_endpoint_rejects_invalid_ids(
    test_client,
) -> None:
    client, _main_app = test_client
    response = client.post(
        "/api/library/classifications/merge",
        json={
            "source_classification_id": "x",
            "target_classification_id": 3,
        },
    )
    assert response.status_code == 400
    assert "must be integers" in response.json().get("detail", "")


def test_library_personalities_overview_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_personality_overview",
        lambda: {
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


def test_library_personalities_table_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "list_personalities",
        lambda **_kwargs: {
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


def test_library_personalities_insights_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_personality_insights",
        lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "script_distribution": [{"script_label": "cyrillic", "mentions_count": 10, "share_pct": 100.0}],
            "variant_clusters": [{"normalized_name": "габдулла тукай", "variants_count": 2}],
            "ambiguous_queue": {"total": 1, "items": [{"raw_name": "Тукай"}]},
        },
    )

    response = client.get("/api/library/personalities/insights")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["script_distribution"][0]["script_label"] == "cyrillic"
    assert payload["ambiguous_queue"]["total"] == 1


def test_library_publishers_overview_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_publisher_overview",
        lambda: {
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


def test_library_publishers_table_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "list_publishers",
        lambda **_kwargs: {
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


def test_library_publishers_insights_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_publisher_insights",
        lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "script_distribution": [{"script_label": "cyrillic", "mentions_count": 15, "share_pct": 100.0}],
            "variant_clusters": [{"normalized_name": "таткнигоиздат", "variants_count": 2}],
            "ambiguous_queue": {"total": 1, "items": [{"raw_name": "Татиздат"}]},
        },
    )

    response = client.get("/api/library/publishers/insights")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["script_distribution"][0]["script_label"] == "cyrillic"
    assert payload["ambiguous_queue"]["total"] == 1


def test_library_collections_overview_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_collection_overview",
        lambda: {
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


def test_library_collections_table_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "list_library_collections",
        lambda **_kwargs: {
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


def test_library_collection_items_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "list_collection_items",
        lambda collection_id, **_kwargs: {
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


def test_library_collection_review_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_collection_review",
        lambda collection_id, **_kwargs: {
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


def test_library_collection_proposals_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client
    monkeypatch.setattr(
        main_app,
        "list_collection_proposals",
        lambda **kwargs: {
            "available": True,
            "page": kwargs["page"],
            "total_pages": 1,
            "total": 1,
            "items": [{"proposal_id": 17, "status": kwargs["status"]}],
        },
    )

    response = client.get("/api/library/collection-proposals?status=review_ready&page=1")

    assert response.status_code == 200
    assert response.json()["items"][0]["proposal_id"] == 17


def test_library_collection_proposal_decision_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client
    monkeypatch.setattr(
        main_app,
        "decide_collection_proposal",
        lambda _db, proposal_id, decision, selected_md5s: {
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


def test_library_collection_update_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "update_collection",
        lambda _db, collection_id, updates: {
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


def test_library_collection_merge_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "merge_collections",
        lambda _db, source_collection_id, target_collection_id: {
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


def test_library_document_open_redirects_to_resolved_storage(test_client, monkeypatch) -> None:
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


def test_library_normalization_overview_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_normalization_dashboard",
        lambda _db, _entity_type: {
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
    monkeypatch.setattr(
        main_app,
        "get_normalization_quality",
        lambda _db, _entity_type: {"available": True, "error": None, "stats": {"coverage_pct": 40.0}},
    )
    monkeypatch.setattr(
        main_app,
        "list_suggestions",
        lambda _db, _entity_type, limit=80: {"available": True, "error": None, "items": [{"raw_name": "Тукай"}][:limit]},
    )
    monkeypatch.setattr(
        main_app,
        "list_normalization_history",
        lambda _db, _entity_type, limit=20: {"available": True, "error": None, "items": [{"event_id": 1}][:limit]},
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


def test_library_normalization_queue_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_review_queue",
        lambda _db, _entity_type, **_kwargs: {
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

    response = client.get("/api/library/normalization/personality/queue?status=all&page=1&page_size=40")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["items"][0]["raw_name"] == "Тукай"
    assert payload["items"][0]["queue_status"] == "unreviewed"


def test_library_normalization_link_decision_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "link_alias",
        lambda _db, _entity_type, **kwargs: {
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


def test_library_normalization_reject_rejects_invalid_suggestion_ids(test_client) -> None:
    client, _main_app = test_client

    response = client.post(
        "/api/library/normalization/personality/decisions/reject",
        json={"raw_name": "Тукай", "suggestion_ids": ["bad"]},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "suggestion_ids must be integers"


def test_library_normalization_bulk_link_rejects_invalid_raw_names_shape(test_client) -> None:
    client, _main_app = test_client

    response = client.post(
        "/api/library/normalization/personality/bulk/link",
        json={"raw_names": "Alias One", "canonical_id": 9},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "raw_names must be a list of strings"


def test_library_normalization_bulk_reject_rejects_invalid_raw_names_shape(test_client) -> None:
    client, _main_app = test_client

    response = client.post(
        "/api/library/normalization/personality/bulk/reject",
        json={"raw_names": "Alias One"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "raw_names must be a list of strings"


def test_library_normalization_refresh_suggestions_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "refresh_suggestions",
        lambda _db, _entity_type, limit, use_gemini: {
            "generated": limit,
            "bands": {"high": 1, "medium": 2, "low": 3},
            "event": {"event_id": 9, "use_gemini": use_gemini},
        },
    )

    response = client.post(
        "/api/library/normalization/publisher/suggestions/refresh",
        json={"limit": 77, "use_gemini": False},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["generated"] == 77
    assert payload["event"]["event_id"] == 9
    assert payload["event"]["use_gemini"] is False


def test_library_normalization_rejects_unknown_entity(test_client) -> None:
    client, _main_app = test_client

    response = client.get("/api/library/normalization/unknown")
    assert response.status_code == 404


def test_library_classification_detail_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_classification_detail",
        lambda classification_id, docs_page, docs_page_size: {
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

    response = client.get("/api/library/classifications/7?docs_page=1&docs_page_size=20")
    assert response.status_code == 200
    payload = response.json()
    assert payload["detail"]["available"] is True
    assert payload["detail"]["classification"]["classification_id"] == 7
    assert payload["detail"]["linked_docs"]["items"][0]["md5"] == "abc"
