"""Tests for app wiring registries/dependency contracts."""

from __future__ import annotations

from types import SimpleNamespace

from app.dependencies import (
    build_classification_operations,
    build_entities_operations,
    build_normalization_operations,
    build_route_payload_builders,
)
from app.registry import build_startup_seed_registry


def test_startup_seed_registry_uses_injected_factories() -> None:
    settings = SimpleNamespace(
        shayan=SimpleNamespace(),
        maintenance=SimpleNamespace(),
    )
    panel_defs = [{"panel_id": "x", "title": "X"}]

    registry = build_startup_seed_registry(
        settings,
        panel_defs=panel_defs,
        shayan_task_definitions=lambda _cfg: [{"task_id": "a"}],
        maintenance_task_definitions=lambda _cfg: [{"task_id": "b"}],
        shayan_workflow_bundle=lambda _cfg: {"workflow_id": "w1"},
        maintenance_backup_full_workflow_bundle=lambda: {"workflow_id": "w2"},
        maintenance_backup_incr_workflow_bundle=lambda: {"workflow_id": "w3"},
        library_workflow_bundle=lambda: {"workflow_id": "w4"},
        library_personality_normalization_workflow_bundle=lambda: {"workflow_id": "w5"},
        library_publisher_normalization_workflow_bundle=lambda: {"workflow_id": "w6"},
    )

    assert registry.panel_defs == panel_defs
    assert [item["task_id"] for item in registry.task_defs] == ["a", "b"]
    assert [item["workflow_id"] for item in registry.workflow_bundles] == [
        "w1",
        "w2",
        "w3",
        "w4",
        "w5",
        "w6",
    ]


def test_route_payload_builders_bind_payload_builder_methods() -> None:
    class _FakeBuilder:
        def build_dashboard_payload(self):
            return {"ok": "dashboard"}

        def build_schedules_payload(self):
            return {"ok": "schedules"}

        def build_tasks_payload(self):
            return {"ok": "tasks"}

        def build_task_detail_payload(self, task_key: str, *, limit: int = 20):
            return {"task_key": task_key, "limit": limit}

        def build_flow_detail_payload(self, flow_key: str, *, limit_per_task: int = 20):
            return {"flow_key": flow_key, "limit_per_task": limit_per_task}

        def build_library_payload(self):
            return {"ok": "library"}

        def build_database_state_payload(self):
            return {"ok": "db"}

        def build_classification_detail_payload(
            self,
            classification_id: int,
            *,
            docs_page: int = 1,
            docs_page_size: int = 40,
        ):
            return {
                "classification_id": classification_id,
                "docs_page": docs_page,
                "docs_page_size": docs_page_size,
            }

        def build_personality_payload(self):
            return {"ok": "personality"}

        def build_publisher_payload(self):
            return {"ok": "publisher"}

        def build_normalization_payload(self, entity_type: str):
            return {"entity_type": entity_type}

        def build_collections_payload(self):
            return {"ok": "collections"}

    builders = build_route_payload_builders(_FakeBuilder())
    assert builders.build_dashboard_payload() == {"ok": "dashboard"}
    assert builders.build_task_detail_payload("abc", limit=7) == {"task_key": "abc", "limit": 7}
    assert builders.build_flow_detail_payload("flow", limit_per_task=9) == {
        "flow_key": "flow",
        "limit_per_task": 9,
    }
    assert builders.build_classification_detail_payload(11, docs_page=2, docs_page_size=50) == {
        "classification_id": 11,
        "docs_page": 2,
        "docs_page_size": 50,
    }
    assert builders.build_normalization_payload("personality") == {"entity_type": "personality"}


def test_route_operation_services_expose_expected_attributes() -> None:
    normalization = build_normalization_operations()
    classification = build_classification_operations()
    entities = build_entities_operations()

    assert callable(normalization.get_review_queue)
    assert callable(normalization.list_canonicals)
    assert callable(normalization.create_canonical)
    assert callable(normalization.link_alias)
    assert callable(normalization.create_and_link_alias)
    assert callable(normalization.reject_alias)
    assert callable(normalization.bulk_link_aliases)
    assert callable(normalization.bulk_reject_aliases)
    assert callable(normalization.list_suggestions)
    assert callable(normalization.refresh_suggestions)
    assert callable(normalization.get_normalization_merge_candidates)
    assert callable(normalization.merge_canonicals)
    assert callable(normalization.list_normalization_history)
    assert callable(normalization.undo_event)
    assert callable(normalization.get_normalization_quality)
    assert callable(normalization.get_normalization_evidence)

    assert callable(classification.list_classifications)
    assert callable(classification.get_classification_insights)
    assert callable(classification.get_normalization_preview)
    assert callable(classification.get_merge_candidates)

    assert callable(entities.list_personalities)
    assert callable(entities.get_personality_insights)
    assert callable(entities.list_publishers)
    assert callable(entities.get_publisher_insights)
    assert callable(entities.list_library_collections)
    assert callable(entities.get_collection_insights)
    assert callable(entities.list_collection_items)
    assert callable(entities.update_collection)
