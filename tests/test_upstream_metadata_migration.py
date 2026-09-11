"""Schema coverage for database-owned upstream document metadata."""

import importlib.util
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_upstream_metadata_is_migrated_out_of_document(prepared_test_schema) -> None:
    database_url, schema = prepared_test_schema
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert inspector.has_table("library_upstream_metadata", schema=schema)
        columns = {
            item["name"]
            for item in inspector.get_columns("library_upstream_metadata", schema=schema)
        }
        assert columns == {"md5", "payload_json"}
        if inspector.has_table("document", schema=schema):
            document_columns = {
                item["name"]
                for item in inspector.get_columns("document", schema=schema)
            }
            assert "upstream_meta_url" not in document_columns
    finally:
        engine.dispose()


def test_upstream_metadata_migration_handles_legacy_public_document() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260830_0041_store_upstream_metadata.py"
    )
    spec = importlib.util.spec_from_file_location(
        "store_upstream_metadata_migration",
        migration_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    constants = "\n".join(
        value for value in module.upgrade.__code__.co_consts if isinstance(value, str)
    )
    assert "to_regclass('public.document')" in constants
    assert "target_schema := 'public'" in constants


def test_simplification_preserves_existing_payload(prepared_test_schema) -> None:
    database_url, _ = prepared_test_schema
    schema = f"manzara_upstream_metadata_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    md5 = "a" * 32
    try:
        command.upgrade(config, "20260908_0046")
        with engine.begin() as conn:
            conn.execute(
                text(f'INSERT INTO "{schema}".document (md5) VALUES (:md5)'),
                {"md5": md5},
            )
            conn.execute(
                text(
                    f'''INSERT INTO "{schema}".library_upstream_metadata (
                        md5, payload_json, source_key, source_etag,
                        source_size, payload_sha256
                    ) VALUES (:md5, :payload, :key, 'etag', 12, :sha)'''
                ),
                {
                    "md5": md5,
                    "payload": '{"title": "Kitap"}',
                    "key": f"{md5}.json",
                    "sha": "b" * 64,
                },
            )

        command.upgrade(config, "head")

        with engine.begin() as conn:
            row = conn.execute(
                text(
                    f'SELECT md5, payload_json '
                    f'FROM "{schema}".library_upstream_metadata'
                )
            ).mappings().one()
        assert row["md5"] == md5
        assert row["payload_json"] == {"title": "Kitap"}
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
