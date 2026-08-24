"""Library PDF preview domain and task contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.modules.library.previews import (
    PREVIEW_RECIPE_VERSION,
    build_preview_api_payload,
    derive_preview_status,
    preview_object_key,
    select_preview_pages,
)
from app.modules.library.tasks import (
    LIBRARY_GENERATE_BOOK_PREVIEWS_TASK_ID,
    library_task_definitions,
)
from app.modules.library.preview_repository import LibraryPreviewRepository


@pytest.mark.parametrize(
    ("page_count", "expected"),
    [
        (1, [("first", 1, "1")]),
        (2, [("first", 1, "1"), ("last", 2, "l")]),
        (3, [("first", 1, "1"), ("second", 2, "2"), ("last", 3, "l")]),
        (287, [("first", 1, "1"), ("second", 2, "2"), ("last", 287, "l")]),
    ],
)
def test_select_preview_pages_uses_distinct_semantic_roles(
    page_count: int,
    expected: list[tuple[str, int, str]],
) -> None:
    selected = select_preview_pages(page_count)

    assert [(item.role, item.page_number, item.object_alias) for item in selected] == expected


def test_select_preview_pages_rejects_empty_documents() -> None:
    with pytest.raises(ValueError, match="at least one page"):
        select_preview_pages(0)


@pytest.mark.parametrize(
    ("alias", "variant", "suffix"),
    [
        ("1", "small", "/1s.webp"),
        ("1", "large", "/1l.webp"),
        ("2", "small", "/2s.webp"),
        ("2", "large", "/2l.webp"),
        ("l", "small", "/ls.webp"),
        ("l", "large", "/ll.webp"),
    ],
)
def test_preview_object_key_uses_compact_aliases(alias: str, variant: str, suffix: str) -> None:
    md5 = "abcdef0123456789abcdef0123456789"

    key = preview_object_key(md5, alias, variant)

    assert key == f"{md5}{suffix}"


def test_ready_status_uses_verified_object_count_for_page_count() -> None:
    assert derive_preview_status(1, 2) == "ready"
    assert derive_preview_status(2, 2) == "partial"


def test_preview_api_payload_exposes_variable_collection_and_actual_last_page() -> None:
    md5 = "abcdef0123456789abcdef0123456789"
    row = {
        "md5": md5,
        "recipe_version": "webp-v1",
        "source_page_count": 287,
        "status": "ready",
        "error_text": None,
    }

    payload = build_preview_api_payload(
        row,
        bucket="ttpreviews",
        endpoint_url="https://s3.eu-central-003.backblazeb2.com",
    )

    assert payload["expected_preview_count"] == 3
    assert payload["preview_count"] == 3
    assert [item["role"] for item in payload["previews"]] == ["first", "second", "last"]
    assert payload["previews"][-1]["page_number"] == 287
    assert payload["previews"][-1]["variants"]["large"]["url"].endswith(
        f"/ttpreviews/{md5}/ll.webp"
    )


def test_library_preview_task_is_registered_from_library_module(tmp_path: Path) -> None:
    tasks = library_task_definitions(app_root=tmp_path)
    task = next(item for item in tasks if item["task_id"] == LIBRARY_GENERATE_BOOK_PREVIEWS_TASK_ID)

    assert task["panel_id"] == "library"
    assert task["task_type"] == "preview"
    assert "run_generate_book_previews.py" in task["command"]["value"]


def test_library_tasks_exclude_local_llm_tasks(tmp_path: Path) -> None:
    tasks = library_task_definitions(app_root=tmp_path)
    task_ids = {str(task["task_id"]) for task in tasks}

    assert "library.collection_triage_benchmark" not in task_ids
    assert "library.collection_triage_smoke" not in task_ids


def test_library_cleanup_task_runs_as_importable_module(tmp_path: Path) -> None:
    tasks = library_task_definitions(app_root=tmp_path)
    task = next(
        item for item in tasks if item["task_id"] == "library.prepare_document_cleanup"
    )

    assert task["panel_id"] == "maintenance"
    assert task["title"] == "Cleanup plan"
    assert "-m app.modules.library.runtime.run_prepare_document_cleanup" in task[
        "command"
    ]["value"]


def test_preview_repository_checkpoints_and_aggregates_coverage(
    test_client: tuple[object, object],
    prepared_test_schema: tuple[str, str],
) -> None:
    _ = test_client
    database_url, schema = prepared_test_schema
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(f'SET search_path TO "{schema}", public'))
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS document (
                        md5 TEXT PRIMARY KEY,
                        mime_type TEXT,
                        document_url TEXT,
                        sharing_restricted BOOLEAN
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS metadata (
                        md5 TEXT PRIMARY KEY REFERENCES document(md5) ON DELETE CASCADE,
                        lib BOOLEAN
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO document (md5, mime_type, document_url, sharing_restricted)
                    VALUES (:first, 'application/pdf', :first_url, FALSE),
                           (:second, 'application/pdf', :second_url, FALSE),
                           (:restricted, 'application/pdf', :restricted_url, TRUE),
                           (:ignored, 'text/plain', :ignored_url, FALSE)
                    """
                ),
                {
                    "first": "11111111111111111111111111111111",
                    "first_url": "https://s3.test/public/11111111111111111111111111111111.pdf",
                    "second": "22222222222222222222222222222222",
                    "second_url": "https://s3.test/public/22222222222222222222222222222222.pdf",
                    "restricted": "44444444444444444444444444444444",
                    "restricted_url": "https://s3.test/private/44444444444444444444444444444444.pdf",
                    "ignored": "33333333333333333333333333333333",
                    "ignored_url": "https://s3.test/public/33333333333333333333333333333333.txt",
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO metadata (md5, lib)
                    VALUES (:first, TRUE), (:second, TRUE), (:restricted, TRUE),
                           (:ignored, TRUE)
                    """
                ),
                {
                    "first": "11111111111111111111111111111111",
                    "second": "22222222222222222222222222222222",
                    "restricted": "44444444444444444444444444444444",
                    "ignored": "33333333333333333333333333333333",
                },
            )
    finally:
        engine.dispose()

    repository = LibraryPreviewRepository(database_url, schema=schema)
    candidates = repository.list_candidates(
        recipe_version=PREVIEW_RECIPE_VERSION,
        endpoint_url="https://s3.test",
        public_bucket="public",
    )
    assert [item["md5"] for item in candidates] == [
        "11111111111111111111111111111111",
        "22222222222222222222222222222222",
    ]

    md5 = candidates[0]["md5"]
    repository.start_attempt(md5, recipe_version=PREVIEW_RECIPE_VERSION, run_id=None)
    repository.checkpoint(
        md5,
        recipe_version=PREVIEW_RECIPE_VERSION,
        source_page_count=1,
        status="ready",
        run_id=None,
    )

    stored = repository.get(md5)
    assert stored is not None
    assert stored["status"] == "ready"
    assert "manifest" not in stored
    assert stored["attempt_count"] == 1

    stats = repository.get_stats(
        recipe_version=PREVIEW_RECIPE_VERSION,
        endpoint_url="https://s3.test",
        public_bucket="public",
    )
    assert stats == {
        "recipe_version": PREVIEW_RECIPE_VERSION,
        "eligible": 2,
        "ready": 1,
        "pending": 1,
        "partial": 0,
        "failed": 0,
        "generated_preview_pages": 1,
        "generated_image_objects": 2,
    }

    assert [item["md5"] for item in repository.list_candidates(
        recipe_version=PREVIEW_RECIPE_VERSION,
        endpoint_url="https://s3.test",
        public_bucket="public",
    )] == ["22222222222222222222222222222222"]
