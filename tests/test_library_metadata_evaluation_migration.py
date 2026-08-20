"""Metadata evaluation checkpoint migration tests."""

from __future__ import annotations

from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_metadata_evaluation_state_table_exists(
    prepared_test_schema,  # noqa: ANN001
) -> None:
    database_url, schema = prepared_test_schema
    engine = create_engine(database_url)
    inspector = inspect(engine)

    try:
        assert inspector.has_table(
            "library_metadata_evaluation_state",
            schema=schema,
        )
        columns = {
            item["name"]
            for item in inspector.get_columns(
                "library_metadata_evaluation_state",
                schema=schema,
            )
        }
        assert {
            "md5",
            "status",
            "attempts_json",
            "model_pool_json",
            "last_run_id",
            "terminal_reason",
        } <= columns
    finally:
        engine.dispose()


def test_metadata_evaluation_state_table_is_repaired_when_head_is_missing_it(
    prepared_test_schema: tuple[str, str],
) -> None:
    database_url, _prepared_schema = prepared_test_schema
    schema = f"manzara_metadata_eval_repair_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    try:
        command.upgrade(config, "20260816_0029")
        with engine.begin() as conn:
            conn.execute(
                text(f'DROP TABLE "{schema}".library_metadata_evaluation_state')
            )

        command.upgrade(config, "head")

        assert inspect(engine).has_table(
            "library_metadata_evaluation_state",
            schema=schema,
        )
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
