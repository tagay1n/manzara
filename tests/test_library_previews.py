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

    assert key.startswith(f"library/pdf-three-page-webp-v1/{md5[:2]}/{md5}/")
    assert key.endswith(suffix)


def test_ready_status_uses_page_count_instead_of_assuming_six_objects() -> None:
    one_page_manifest = {
        "first": {
            "page_number": 1,
            "variants": {
                "small": {"key": "1s.webp"},
                "large": {"key": "1l.webp"},
            },
        }
    }
    assert derive_preview_status(1, one_page_manifest) == "ready"
    assert derive_preview_status(2, one_page_manifest) == "partial"


def test_preview_api_payload_exposes_variable_collection_and_actual_last_page() -> None:
    md5 = "abcdef0123456789abcdef0123456789"
    row = {
        "md5": md5,
        "recipe_version": "pdf-three-page-webp-v1",
        "source_page_count": 287,
        "status": "ready",
        "manifest": {
            "first": {
                "page_number": 1,
                "variants": {
                    "small": {"key": "prefix/1s.webp", "width": 400, "height": 566},
                    "large": {"key": "prefix/1l.webp", "width": 1000, "height": 1415},
                },
            },
            "second": {
                "page_number": 2,
                "variants": {
                    "small": {"key": "prefix/2s.webp", "width": 400, "height": 566},
                    "large": {"key": "prefix/2l.webp", "width": 1000, "height": 1415},
                },
            },
            "last": {
                "page_number": 287,
                "variants": {
                    "small": {"key": "prefix/ls.webp", "width": 400, "height": 566},
                    "large": {"key": "prefix/ll.webp", "width": 1000, "height": 1415},
                },
            },
        },
        "error_text": None,
    }

    payload = build_preview_api_payload(row, bucket="ttbook-previews")

    assert payload["expected_preview_count"] == 3
    assert payload["preview_count"] == 3
    assert [item["role"] for item in payload["previews"]] == ["first", "second", "last"]
    assert payload["previews"][-1]["page_number"] == 287
    assert payload["previews"][-1]["variants"]["large"]["url"].endswith("/prefix/ll.webp")


def test_library_preview_task_is_registered_from_library_module(tmp_path: Path) -> None:
    tasks = library_task_definitions(app_root=tmp_path)
    task = next(item for item in tasks if item["task_id"] == LIBRARY_GENERATE_BOOK_PREVIEWS_TASK_ID)

    assert task["panel_id"] == "library"
    assert task["task_type"] == "preview"
    assert "run_generate_book_previews.py" in task["command"]["value"]


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
                        document_url TEXT
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
                    INSERT INTO document (md5, mime_type, document_url)
                    VALUES (:first, 'application/pdf', :first_url),
                           (:second, 'application/pdf', :second_url),
                           (:ignored, 'text/plain', :ignored_url)
                    """
                ),
                {
                    "first": "11111111111111111111111111111111",
                    "first_url": "11111111111111111111111111111111.pdf",
                    "second": "22222222222222222222222222222222",
                    "second_url": "22222222222222222222222222222222.pdf",
                    "ignored": "33333333333333333333333333333333",
                    "ignored_url": "33333333333333333333333333333333.txt",
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO metadata (md5, lib)
                    VALUES (:first, TRUE), (:second, TRUE), (:ignored, TRUE)
                    """
                ),
                {
                    "first": "11111111111111111111111111111111",
                    "second": "22222222222222222222222222222222",
                    "ignored": "33333333333333333333333333333333",
                },
            )
    finally:
        engine.dispose()

    repository = LibraryPreviewRepository(database_url, schema=schema)
    candidates = repository.list_candidates(recipe_version=PREVIEW_RECIPE_VERSION)
    assert [item["md5"] for item in candidates] == [
        "11111111111111111111111111111111",
        "22222222222222222222222222222222",
    ]

    md5 = candidates[0]["md5"]
    repository.start_attempt(md5, recipe_version=PREVIEW_RECIPE_VERSION, run_id=None)
    manifest = {
        "first": {
            "page_number": 1,
            "variants": {
                "small": {"key": "prefix/1s.webp"},
                "large": {"key": "prefix/1l.webp"},
            },
        }
    }
    repository.checkpoint(
        md5,
        recipe_version=PREVIEW_RECIPE_VERSION,
        source_page_count=1,
        status="ready",
        manifest=manifest,
        run_id=None,
    )

    stored = repository.get(md5)
    assert stored is not None
    assert stored["status"] == "ready"
    assert stored["manifest"] == manifest
    assert stored["attempt_count"] == 1

    stats = repository.get_stats(recipe_version=PREVIEW_RECIPE_VERSION)
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
        recipe_version=PREVIEW_RECIPE_VERSION
    )] == ["22222222222222222222222222222222"]
