from __future__ import annotations

from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.db import Database
from app.modules.library import collection_catalog


def test_collection_redesign_migration_has_path_free_proposal_tables(
    prepared_test_schema,
) -> None:
    database_url, _prepared_schema = prepared_test_schema
    schema = f"manzara_collection_redesign_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    try:
        command.upgrade(config, "head")
        inspector = inspect(engine)
        assert inspector.has_table(
            "library_collection_document_features", schema=schema
        )
        assert inspector.has_table("library_collection_proposals", schema=schema)
        assert inspector.has_table("library_collection_proposal_items", schema=schema)
        assert inspector.has_table(
            "library_collection_validation_attempts", schema=schema
        )
        assert not inspector.has_table(
            "library_collection_source_aliases", schema=schema
        )
        collection_columns = {
            item["name"]
            for item in inspector.get_columns("library_collections", schema=schema)
        }
        item_columns = {
            item["name"]
            for item in inspector.get_columns("library_collection_items", schema=schema)
        }
        assert "source_key" not in collection_columns
        assert "item_hint" not in item_columns
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_merge_collection_moves_memberships_and_signatures(
    prepared_test_schema, monkeypatch
) -> None:
    database_url, schema = prepared_test_schema
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{schema}", public'))
        conn.execute(
            text(
                "TRUNCATE library_collection_validation_attempts, library_collection_proposal_items, library_collection_proposals, library_collection_signatures, library_collection_items, library_collections RESTART IDENTITY CASCADE"
            )
        )
        rows = (
            conn.execute(
                text("""
            INSERT INTO library_collections (
                title, normalized_title, include_in_library, metadata_template_json,
                notes, applied_at, created_at, updated_at
            ) VALUES
                ('Canonical', 'canonical', 1, '{}', '', NULL, 'now', 'now'),
                ('Variant', 'variant', 1, '{}', '', NULL, 'now', 'now')
            RETURNING collection_id, title
        """)
            )
            .mappings()
            .all()
        )
        ids = {row["title"]: int(row["collection_id"]) for row in rows}
        conn.execute(
            text("""
            INSERT INTO library_collection_items (collection_id, md5, item_title, created_at, updated_at)
            VALUES (:target, 'target-md5', 'Target', 'now', 'now'),
                   (:source, 'source-md5', 'Source', 'now', 'now')
        """),
            {"target": ids["Canonical"], "source": ids["Variant"]},
        )
        conn.execute(
            text("""
            INSERT INTO library_collection_signatures (
                collection_id, signature_type, normalized_value, provenance, created_at, updated_at
            ) VALUES (:source, 'canonical_title', 'variant', 'test', 'now', 'now')
        """),
            {"source": ids["Variant"]},
        )

    monkeypatch.setenv("MANZARA_DB_SCHEMA", schema)
    monkeypatch.setattr(
        collection_catalog,
        "create_runtime_engine",
        lambda: (create_engine(database_url), "test"),
    )
    result = collection_catalog.merge_collections(
        Database(database_url, schema=schema),
        source_collection_id=ids["Variant"],
        target_collection_id=ids["Canonical"],
    )

    assert result["moved_items"] == 1
    with engine.connect() as conn:
        conn.execute(text(f'SET search_path TO "{schema}", public'))
        assert (
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM library_collections WHERE collection_id=:id"
                ),
                {"id": ids["Variant"]},
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                text(
                    "SELECT collection_id FROM library_collection_items WHERE md5='source-md5'"
                )
            ).scalar_one()
            == ids["Canonical"]
        )
        assert (
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM library_collection_signatures WHERE collection_id=:id AND normalized_value='variant'"
                ),
                {"id": ids["Canonical"]},
            ).scalar_one()
            == 1
        )
    engine.dispose()
