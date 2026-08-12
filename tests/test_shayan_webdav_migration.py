"""Integration coverage for the Shayan WebDAV checkpoint cutover."""

from __future__ import annotations

from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_webdav_migration_replaces_empty_s3_checkpoint_contract(
    prepared_test_schema: tuple[str, str],
) -> None:
    database_url, _prepared_schema = prepared_test_schema
    schema = f"manzara_webdav_migration_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    try:
        command.upgrade(config, "20260731_0013")
        assert inspect(engine).has_table("shayan_s3_transfers", schema=schema)

        command.upgrade(config, "head")

        inspector = inspect(engine)
        assert not inspector.has_table("shayan_s3_transfers", schema=schema)
        assert inspector.has_table("shayan_webdav_transfers", schema=schema)
        columns = {
            column["name"]
            for column in inspector.get_columns(
                "shayan_webdav_transfers",
                schema=schema,
            )
        }
        assert {"target_path", "target_etag", "target_checksum"} <= columns
        manifest_columns = {
            column["name"]
            for column in inspector.get_columns(
                "shayan_manifest_entries",
                schema=schema,
            )
        }
        assert {
            "webdav_status",
            "webdav_remote_path",
            "webdav_source_md5",
            "webdav_source_size",
            "webdav_target_etag",
            "webdav_target_checksum",
            "webdav_uploaded_payload_hash",
        } <= manifest_columns
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_direct_upload_migration_leaves_yandex_rows_for_migration_task(
    prepared_test_schema: tuple[str, str],
) -> None:
    database_url, _prepared_schema = prepared_test_schema
    schema = f"manzara_direct_upload_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    try:
        command.upgrade(config, "20260812_0022")
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".shayan_manifest_entries (
                        entry_key, payload_json, payload_hash,
                        yadisk_status, yadisk_remote_path,
                        yadisk_uploaded_payload_hash, created_at, updated_at
                    ) VALUES (
                        'episode-1', '{{}}', 'payload-1',
                        'uploaded', '/yandex/episode-1.mkv',
                        'payload-1', '2026-08-12T00:00:00+00:00',
                        '2026-08-12T00:00:00+00:00'
                    )
                    """
                )
            )

        command.upgrade(config, "head")

        with engine.connect() as conn:
            status = conn.execute(
                text(
                    f"SELECT webdav_status FROM \"{schema}\".shayan_manifest_entries "
                    "WHERE entry_key = 'episode-1'"
                )
            ).scalar_one()
        assert status == "legacy_yadisk"
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
