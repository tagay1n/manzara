"""Schema coverage for the singleton task conveyor."""

from __future__ import annotations

from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_conveyor_tables_and_meaningful_result_column_exist(
    prepared_test_schema: tuple[str, str],
) -> None:
    database_url, _prepared_schema = prepared_test_schema
    schema = f"manzara_conveyor_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    try:
        command.upgrade(config, "head")
        inspector = inspect(engine)
        assert inspector.has_table("conveyor_definitions", schema=schema)
        assert inspector.has_table("conveyor_runs", schema=schema)
        assert inspector.has_table("conveyor_run_items", schema=schema)
        task_columns = {
            column["name"]
            for column in inspector.get_columns("task_definitions", schema=schema)
        }
        assert "meaningful_result_json" in task_columns
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
