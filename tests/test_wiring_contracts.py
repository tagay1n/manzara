"""Tests for app wiring registries/dependency contracts."""

from __future__ import annotations

from types import SimpleNamespace

from app.dependencies import (
    build_classification_operations,
    build_entities_operations,
    build_normalization_operations,
    build_route_payload_builders,
)
from app.modules.library.collection_tasks import collection_task_definitions
from app.modules.maintenance.config import MaintenanceSettings
from app.modules.maintenance.tasks import maintenance_task_definitions
from app.registry import build_startup_seed_registry


def test_maintenance_task_definitions_include_guarded_sync_task(tmp_path) -> None:
    tasks = maintenance_task_definitions(
        MaintenanceSettings(
            monocorpus_repo_path=tmp_path,
            pgbackrest_stanza="monocorpus",
        )
    )

    by_id = {str(task["task_id"]): task for task in tasks}
    task_ids = set(by_id)
    assert "maintenance.monocorpus_sync" in task_ids
    assert by_id["maintenance.monocorpus_sync"]["title"] == "Sync"
    assert (
        by_id["maintenance.sync_documents_s3"]["title"]
        == "Upload to Backblaze S3"
    )
    assert not any(task_id.startswith("library.collection_") for task_id in task_ids)


def test_collection_tasks_belong_to_dedicated_flow(tmp_path) -> None:
    tasks = collection_task_definitions(app_root=tmp_path)

    assert {task["task_id"] for task in tasks} == {
        "library.collection_detect",
        "library.collection_validate",
        "library.collection_apply",
    }
    assert {task["panel_id"] for task in tasks} == {"collections"}


def test_startup_seed_registry_contains_only_panels_and_tasks() -> None:
    settings = SimpleNamespace(
        maintenance=SimpleNamespace(),
    )
    panel_defs = [{"panel_id": "x", "title": "X"}]

    registry = build_startup_seed_registry(
        settings,
        panel_defs=panel_defs,
        maintenance_task_definitions=lambda _cfg: [{"task_id": "a"}],
        library_task_definitions=lambda: [{"task_id": "b"}],
        collection_task_definitions=lambda: [{"task_id": "c"}],
    )

    assert registry.panel_defs == panel_defs
    assert [item["task_id"] for item in registry.task_defs] == ["a", "b", "c"]
    assert not hasattr(registry, "workflow_bundles")


def test_route_payload_builders_bind_payload_builder_methods() -> None:
    class _FakeBuilder:
        def build_system_state_payload(self):
            return {"ok": "system"}

        def build_dashboard_payload(self):
            return {"ok": "dashboard"}

        def build_tasks_payload(self):
            return {"ok": "tasks"}

        def build_task_detail_payload(self, task_key: str, *, limit: int = 20):
            return {"task_key": task_key, "limit": limit}

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
    assert builders.build_system_state_payload() == {"ok": "system"}
    assert builders.build_dashboard_payload() == {"ok": "dashboard"}
    assert builders.build_task_detail_payload("abc", limit=7) == {"task_key": "abc", "limit": 7}
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
    assert callable(classification.merge_classifications)

    assert callable(entities.list_personalities)
    assert callable(entities.get_personality_insights)
    assert callable(entities.list_publishers)
    assert callable(entities.get_publisher_insights)
    assert callable(entities.list_library_collections)
    assert callable(entities.get_collection_insights)
    assert callable(entities.get_collection_review)
    assert callable(entities.list_collection_items)
    assert callable(entities.update_collection)
