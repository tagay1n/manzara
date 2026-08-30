"""Schema coverage for database-owned upstream document metadata."""

import importlib.util
from pathlib import Path

from sqlalchemy import create_engine, inspect


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
        assert columns == {
            "md5",
            "payload_json",
            "source_key",
            "source_etag",
            "source_size",
            "source_last_modified",
            "payload_sha256",
            "imported_at",
            "updated_at",
        }
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
