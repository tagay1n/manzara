"""Integration coverage for document primary-storage migration."""

from __future__ import annotations

from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_document_verification_columns_are_added_to_existing_document_table(
    prepared_test_schema: tuple[str, str],
) -> None:
    database_url, _prepared_schema = prepared_test_schema
    schema = f"manzara_document_migration_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    try:
        command.upgrade(config, "20260731_0012")
        with engine.begin() as conn:
            conn.execute(
                text(
                    f'''CREATE TABLE "{schema}".document (
                        md5 TEXT PRIMARY KEY,
                        document_url TEXT
                    )'''
                )
            )
        command.upgrade(config, "head")
        columns = {
            column["name"]
            for column in inspect(engine).get_columns("document", schema=schema)
        }
        assert {
            "primary_storage_size",
            "primary_storage_etag",
            "primary_storage_verified_at",
        }.issubset(columns)
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
