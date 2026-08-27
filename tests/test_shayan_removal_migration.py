"""Retired Shayan persistence is removed after its standalone export."""

from __future__ import annotations

from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_alembic_head_drops_retired_shayan_tables(prepared_test_schema) -> None:
    database_url, _prepared_schema = prepared_test_schema
    schema = f"manzara_remove_shayan_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    try:
        command.upgrade(config, "20260827_0036")
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".shayan_manifest_entries (
                        entry_key, payload_json, payload_hash, created_at, updated_at
                    ) VALUES ('episode', '{{}}', 'hash', 'now', 'now');
                    INSERT INTO "{schema}".shayan_snapshots (
                        source, generated_at, entries_count, created_at, updated_at
                    ) VALUES ('test', 'now', 1, 'now', 'now');
                    INSERT INTO "{schema}".shayan_snapshot_entries (
                        snapshot_id, entry_key, payload_json, payload_hash, created_at
                    ) VALUES (1, 'episode', '{{}}', 'hash', 'now');
                    """
                )
            )

        command.upgrade(config, "head")

        inspector = inspect(engine)
        for table in (
            "shayan_run_changes",
            "shayan_snapshot_entries",
            "shayan_snapshots",
            "shayan_manifest_entries",
        ):
            assert not inspector.has_table(table, schema=schema)
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
