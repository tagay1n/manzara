from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.db import Database
from app.modules.library import collections


def test_collection_alias_migration_has_durable_identity_constraints(
    prepared_test_schema,
) -> None:
    database_url, _prepared_schema = prepared_test_schema
    schema = f"manzara_collection_alias_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    try:
        command.upgrade(config, "head")
        inspector = inspect(engine)
        assert inspector.has_table("library_collection_source_aliases", schema=schema)
        primary_key = inspector.get_pk_constraint(
            "library_collection_source_aliases",
            schema=schema,
        )
        assert primary_key["constrained_columns"] == ["source_key"]
        foreign_keys = inspector.get_foreign_keys(
            "library_collection_source_aliases",
            schema=schema,
        )
        assert any(
            foreign_key["referred_table"] == "library_collections"
            and foreign_key["constrained_columns"] == ["collection_id"]
            for foreign_key in foreign_keys
        )
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


@pytest.fixture()
def collection_store(prepared_test_schema, monkeypatch):
    database_url, schema = prepared_test_schema
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{schema}", public'))
        conn.execute(text("TRUNCATE library_collection_source_aliases, library_collection_items, library_collections RESTART IDENTITY CASCADE"))
        rows = conn.execute(
            text(
                """
                INSERT INTO library_collections (
                    source_key, title, normalized_title, status, include_in_library,
                    confidence, item_count, heuristics_json, metadata_template_json,
                    notes, last_detected_at, applied_at, created_at, updated_at
                ) VALUES
                    ('folder|canonical', 'Canonical title', 'canonical title', 'approved', 1,
                     0.99, 1, :canonical_heuristics, '{"name":"Canonical metadata"}',
                     'reviewed', 'now', NULL, 'now', 'now'),
                    ('folder|ocr-variant', 'OCR title', 'ocr title', 'suggested', 1,
                     0.91, 2, :source_heuristics, '{"name":"Source metadata"}',
                     '', 'now', NULL, 'now', 'now')
                RETURNING collection_id, source_key
                """
            ),
            {
                "canonical_heuristics": json.dumps({"parent": "folder", "stem": "canonical"}),
                "source_heuristics": json.dumps({"parent": "folder", "stem": "ocr-variant"}),
            },
        ).mappings().all()
        ids = {row["source_key"]: int(row["collection_id"]) for row in rows}
        conn.execute(
            text(
                """
                INSERT INTO library_collection_items (
                    collection_id, md5, item_title, item_hint, signal_json, created_at, updated_at
                ) VALUES
                    (:target_id, 'target-md5', 'Target item', '/folder/target.pdf', '{}', 'now', 'now'),
                    (:source_id, 'source-md5-1', 'Source item 1', '/folder/source-1.pdf', '{}', 'now', 'now'),
                    (:source_id, 'source-md5-2', 'Source item 2', '/folder/source-2.pdf', '{}', 'now', 'now')
                """
            ),
            {
                "target_id": ids["folder|canonical"],
                "source_id": ids["folder|ocr-variant"],
            },
        )

    monkeypatch.setenv("MANZARA_DB_SCHEMA", schema)
    monkeypatch.setattr(
        collections,
        "create_runtime_engine",
        lambda: (create_engine(database_url), "test"),
    )
    yield database_url, schema, ids
    engine.dispose()


def test_merge_collection_preserves_target_and_aliases_source(collection_store) -> None:
    database_url, schema, ids = collection_store
    db = Database(database_url, schema=schema)

    result = collections.merge_collections(
        db,
        source_collection_id=ids["folder|ocr-variant"],
        target_collection_id=ids["folder|canonical"],
    )

    assert result["ok"] is True
    assert result["moved_items"] == 2
    assert result["target"]["status"] == "approved"
    assert result["target"]["title"] == "Canonical title"
    assert result["target"]["item_count"] == 3

    engine = create_engine(database_url)
    with engine.connect() as conn:
        conn.execute(text(f'SET search_path TO "{schema}", public'))
        source_count = conn.execute(
            text("SELECT COUNT(*) FROM library_collections WHERE collection_id = :source_id"),
            {"source_id": ids["folder|ocr-variant"]},
        ).scalar_one()
        alias_owner = conn.execute(
            text("SELECT collection_id FROM library_collection_source_aliases WHERE source_key = 'folder|ocr-variant'")
        ).scalar_one()
        memberships = conn.execute(
            text("SELECT md5 FROM library_collection_items WHERE collection_id = :target_id ORDER BY md5"),
            {"target_id": ids["folder|canonical"]},
        ).scalars().all()
    engine.dispose()

    assert source_count == 0
    assert int(alias_owner) == ids["folder|canonical"]
    assert memberships == ["source-md5-1", "source-md5-2", "target-md5"]


def test_merge_collection_rejects_self_merge(collection_store) -> None:
    database_url, schema, ids = collection_store
    db = Database(database_url, schema=schema)

    with pytest.raises(ValueError, match="different"):
        collections.merge_collections(
            db,
            source_collection_id=ids["folder|canonical"],
            target_collection_id=ids["folder|canonical"],
        )


def test_combine_detected_candidates_routes_alias_to_canonical() -> None:
    candidates = [
        {
            "source_key": "folder|canonical",
            "title": "Canonical",
            "normalized_title": "canonical",
            "confidence": 0.9,
            "item_count": 1,
            "items": [{"md5": "a", "path": "/a"}],
            "heuristics": {"parent": "folder", "stem": "canonical"},
            "metadata_template": {"name": "Canonical"},
        },
        {
            "source_key": "folder|variant",
            "title": "Variant",
            "normalized_title": "variant",
            "confidence": 0.8,
            "item_count": 1,
            "items": [{"md5": "b", "path": "/b"}],
            "heuristics": {"parent": "folder", "stem": "variant"},
            "metadata_template": {"name": "Variant"},
        },
    ]

    combined = collections._combine_detected_candidates(
        candidates,
        primary_source_keys={7: "folder|canonical"},
        source_owners={"folder|canonical": 7, "folder|variant": 7},
    )

    assert len(combined) == 1
    assert combined[0]["source_key"] == "folder|canonical"
    assert combined[0]["item_count"] == 2
    assert {item["md5"] for item in combined[0]["items"]} == {"a", "b"}
    assert combined[0]["heuristics"]["source_keys"] == [
        "folder|canonical",
        "folder|variant",
    ]
