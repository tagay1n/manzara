from __future__ import annotations

import json
import tarfile
from pathlib import Path

from app.modules.library.site_export import (
    ExportStorage,
    build_library_export,
    write_export_bundle,
)
from app.modules.library.runtime.run_site_export import run_export
from app.modules.library.tasks import (
    LIBRARY_SITE_EXPORT_TASK_ID,
    library_task_definitions,
)
from app.task_runtime.logging import TaskLoggingMixin
from app.run_summary import build_structured_run_summary


MD5 = "0123456789abcdef0123456789abcdef"


def _schema_org() -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "Book",
        "name": "Туган тел",
        "author": [{"@type": "Person", "name": "Г. Тукай"}],
        "publisher": {
            "@type": "Organization",
            "name": "Таткнигоиздат",
        },
        "datePublished": "2011",
        "isbn": ["978-5-298-02001-2"],
        "inLanguage": "tt-Cyrl",
        "genre": ["Poetry"],
    }


def _candidate(**updates: object) -> dict:
    row = {
        "md5": MD5,
        "mime_type": "application/pdf",
        "full": True,
        "sharing_restricted": False,
        "document_url": f"https://objects.example/public/{MD5}.pdf",
        "content_url": None,
        "primary_storage_size": 1234,
        "primary_storage_verified_at": "2026-09-02T10:00:00+00:00",
        "schema_org": _schema_org(),
        "classification_id": 31,
        "ddc": "894.3",
        "path_en": ["Literature", "Tatar literature"],
        "path_tt": ["Әдәбият", "Татар әдәбияты"],
        "collection_id": 7,
        "collection_title": "Татар шигърияте",
        "collection_include": True,
        "preview_recipe_version": "webp-v2",
        "preview_status": "ready",
        "source_page_count": 128,
        "first_preview_page": 1,
        "second_preview_page": 2,
        "last_preview_page": 128,
        "has_active_corruption": False,
    }
    row.update(updates)
    return row


def _storage() -> ExportStorage:
    return ExportStorage(
        endpoint_url="https://objects.example",
        public_document_bucket="public",
        public_preview_bucket="previews",
        public_content_bucket="content",
    )


def test_library_catalog_registers_site_export_task(tmp_path: Path) -> None:
    task = {
        item["task_id"]: item for item in library_task_definitions(app_root=tmp_path)
    }[LIBRARY_SITE_EXPORT_TASK_ID]

    assert task["panel_id"] == "library"
    assert task["title"] == "Export static library"
    assert task["task_type"] == "export"
    assert "run_site_export" in task["command"]["value"]


def test_export_resolves_public_relations_and_previews() -> None:
    result = build_library_export(
        [_candidate()],
        aliases=[
            {
                "entity_type": "personality",
                "raw_name": "Г. Тукай",
                "decision_status": "linked",
                "canonical_id": 42,
                "display_name": "Габдулла Тукай",
                "canonical_status": "active",
                "merged_into_id": None,
            },
            {
                "entity_type": "publisher",
                "raw_name": "Таткнигоиздат",
                "decision_status": "linked",
                "canonical_id": 19,
                "display_name": "Татарстан китап нәшрияты",
                "canonical_status": "active",
                "merged_into_id": None,
            },
        ],
        storage=_storage(),
    )

    assert result.exclusions == {}
    document = result.documents[0]
    assert document["id"] == f"document:{MD5}"
    assert document["path"].endswith(f"--{MD5[:8]}/")
    assert document["relations"] == {
        "contributors": [
            {
                "entity_id": "personality:42",
                "property": "author",
                "role_name": None,
                "display_name": "Габдулла Тукай",
                "source_name": "Г. Тукай",
            }
        ],
        "publisher_id": "publisher:19",
        "collection_id": "collection:7",
        "classification_id": "classification:31",
    }
    assert document["preview"]["pages"][2]["large_url"].endswith(
        f"/{MD5}/ll.webp"
    )
    assert [item["id"] for item in result.entities] == [
        "personality:42",
        "publisher:19",
    ]
    assert result.collections[0]["document_ids"] == [f"document:{MD5}"]
    assert result.classifications[0]["document_ids"] == [f"document:{MD5}"]


def test_export_excludes_non_public_and_invalid_documents() -> None:
    invalid = _candidate(md5="1" * 32, schema_org={"name": "Invalid"})
    restricted = _candidate(md5="2" * 32, sharing_restricted=True)
    private = _candidate(
        md5="3" * 32,
        document_url="https://objects.example/private/secret.pdf",
    )
    corrupt = _candidate(md5="4" * 32, has_active_corruption=True)

    result = build_library_export(
        [invalid, restricted, private, corrupt], aliases=[], storage=_storage()
    )

    assert result.documents == []
    assert result.exclusions == {
        "active_corruption": 1,
        "invalid_metadata": 1,
        "not_public_storage": 1,
        "sharing_restricted": 1,
    }


def test_export_normalizes_public_text_to_unicode_nfc() -> None:
    schema_org = _schema_org()
    schema_org["name"] = "A\u0308lif"

    result = build_library_export(
        [_candidate(schema_org=schema_org)], aliases=[], storage=_storage()
    )

    assert result.documents[0]["work"]["name"] == "Älif"


def test_bundle_has_versioned_manifest_and_deterministic_jsonl(tmp_path: Path) -> None:
    result = build_library_export([_candidate()], aliases=[], storage=_storage())

    bundle = write_export_bundle(
        result,
        destination=tmp_path / "final",
        generated_at="2026-09-02T12:30:00Z",
    )

    assert bundle.name == "library-export-v1.tar.gz"
    with tarfile.open(bundle, "r:gz") as archive:
        names = archive.getnames()
        assert names == [
            "manifest.json",
            "documents.jsonl",
            "entities.jsonl",
            "collections.jsonl",
            "classifications.jsonl",
            "redirects.jsonl",
        ]
        manifest = json.load(archive.extractfile("manifest.json"))
        document = json.loads(
            archive.extractfile("documents.jsonl").read().decode("utf-8")
        )

    assert manifest["format"] == "manzara-library-export"
    assert manifest["version"] == 1
    assert manifest["metadata_contract"] == "schema-org.v3"
    assert manifest["files"]["documents.jsonl"]["records"] == 1
    assert manifest["statistics"]["documents_published"] == 1
    assert document["md5"] == MD5


def test_runtime_publishes_bundle_summary_and_structured_artifact(tmp_path: Path) -> None:
    class Repository:
        def load_snapshot(self):
            return [_candidate()], []

    summary = run_export(
        repository=Repository(),
        storage=_storage(),
        destination=tmp_path / "run-9",
    )
    artifact = TaskLoggingMixin()._artifact_event_payload(summary)

    assert Path(summary["bundle_path"]).is_file()
    assert summary["documents_published"] == 1
    assert summary["revision"].startswith("sha256:")
    assert len(summary["bundle_sha256"]) == 64
    assert artifact["bundle_path"] == summary["bundle_path"]
    assert artifact["documents_published"] == 1


def test_completed_export_has_a_human_readable_run_summary() -> None:
    summary = build_structured_run_summary(
        task_id="library.site_export",
        panel_id="library",
        status="completed",
        exit_code=0,
        error_text=None,
        stop_mode=None,
        started_at="2026-09-02T12:00:00Z",
        finished_at="2026-09-02T12:01:00Z",
        log_lines=[],
        artifacts={
            "kind": "library.site_export_summary",
            "documents_published": 24036,
            "documents_excluded": 2201,
            "bundle_path": "/tmp/library-export-v1.tar.gz",
            "revision": "sha256:abc",
        },
    )

    assert summary["message"] == "Static Library export completed: 24,036 documents."
    assert {item["label"] for item in summary["highlights"]} == {
        "Published",
        "Excluded",
        "Revision",
        "Bundle",
    }
