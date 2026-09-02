from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_content_migration_checkpoint_tables_are_created(prepared_test_schema) -> None:
    database_url, _ = prepared_test_schema
    schema = f"manzara_content_move_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    try:
        command.upgrade(config, "head")
        tables = set(inspect(engine).get_table_names(schema=schema))
        assert "maintenance_content_migration" in tables
        assert "maintenance_content_migration_images" in tables
        columns = {
            item["name"]
            for item in inspect(engine).get_columns(
                "maintenance_content_migration", schema=schema
            )
        }
        assert {"status", "source_archive_deleted", "error_text"} <= columns
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
