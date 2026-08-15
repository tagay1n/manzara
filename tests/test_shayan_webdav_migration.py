"""Integration coverage for the Shayan WebDAV checkpoint cutover."""

from __future__ import annotations

from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_completed_migration_checkpoint_table_is_removed_but_direct_upload_remains(
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
        assert not inspector.has_table("shayan_webdav_transfers", schema=schema)
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


def test_direct_upload_migration_keeps_legacy_yandex_marker(
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
                    f'SELECT webdav_status FROM "{schema}".shayan_manifest_entries '
                    "WHERE entry_key = 'episode-1'"
                )
            ).scalar_one()
        assert status == "legacy_yadisk"
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_completed_task_history_checkpoints_and_conveyor_references_are_purged(
    prepared_test_schema: tuple[str, str],
) -> None:
    database_url, _prepared_schema = prepared_test_schema
    schema = f"manzara_remove_shayan_migration_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    task_id = "shayan.transfer_yadisk_webdav"
    try:
        command.upgrade(config, "20260815_0025")
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".panel_definitions
                        (panel_id, title, created_at, updated_at)
                    VALUES ('shayan', 'Shayan', 'now', 'now')
                    """
                )
            )
            conn.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".task_definitions (
                        task_id, panel_id, title, task_type, icon_idle, icon_running,
                        command_json, cwd, meaningful_result_json, created_at, updated_at
                    ) VALUES (
                        :task_id, 'shayan', 'Migrate to Hetzner', 'transfer',
                        'CloudCog', 'Square', '{{}}', '/tmp', '{{}}', 'now', 'now'
                    )
                    """
                ),
                {"task_id": task_id},
            )
            run_id = conn.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".runs (
                        task_id, panel_id, status, started_at, created_at, updated_at
                    ) VALUES (:task_id, 'shayan', 'completed', 'now', 'now', 'now')
                    RETURNING run_id
                    """
                ),
                {"task_id": task_id},
            ).scalar_one()
            conn.execute(
                text(
                    f'INSERT INTO "{schema}".run_logs (run_id, stream, line, ts) '
                    "VALUES (:run_id, 'stdout', 'copied', 'now')"
                ),
                {"run_id": run_id},
            )
            conn.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".events
                        (type, task_id, run_id, panel_id, ts, payload_json)
                    VALUES ('task.completed', :task_id, :run_id, 'shayan', 'now', '{{}}')
                    """
                ),
                {"task_id": task_id, "run_id": run_id},
            )
            conn.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".shayan_webdav_transfers (
                        source_path, category, source_md5, source_size, target_path,
                        status, discovered_at, created_at, updated_at
                    ) VALUES ('/source/a.mkv', 'shows', 'abc', 3, '/target/a.mkv',
                              'uploaded', 'now', 'now', 'now')
                    """
                )
            )
            stages = (
                '[{"stage_id":"s1","items":['
                '{"item_id":"old","task_id":"shayan.transfer_yadisk_webdav"},'
                '{"item_id":"keep","task_id":"shayan.upload_yadisk"}]}]'
            )
            conn.execute(
                text(
                    f"""
                    UPDATE "{schema}".conveyor_definitions
                    SET stages_json = :stages
                    WHERE conveyor_id = 'default'
                    """
                ),
                {"stages": stages},
            )
            conveyor_run_id = conn.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".conveyor_runs
                        (definition_revision, status, started_at)
                    VALUES (0, 'completed', 'now') RETURNING conveyor_run_id
                    """
                )
            ).scalar_one()
            conn.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".conveyor_run_items (
                        conveyor_run_id, item_id, stage_id, stage_order, task_order,
                        task_id, status, task_run_id
                    ) VALUES (:conveyor_run_id, 'old', 's1', 0, 0,
                              :task_id, 'completed', :run_id)
                    """
                ),
                {
                    "conveyor_run_id": conveyor_run_id,
                    "task_id": task_id,
                    "run_id": run_id,
                },
            )

        command.upgrade(config, "head")

        with engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        f'SELECT count(*) FROM "{schema}".task_definitions WHERE task_id=:id'
                    ),
                    {"id": task_id},
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text(f'SELECT count(*) FROM "{schema}".runs WHERE task_id=:id'),
                    {"id": task_id},
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text(f'SELECT count(*) FROM "{schema}".events WHERE task_id=:id'),
                    {"id": task_id},
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text(f'SELECT count(*) FROM "{schema}".conveyor_runs'),
                ).scalar_one()
                == 0
            )
            stages = conn.execute(
                text(f'SELECT stages_json FROM "{schema}".conveyor_definitions'),
            ).scalar_one()
            assert task_id not in stages
            assert "shayan.upload_yadisk" in stages
            assert not inspect(engine).has_table(
                "shayan_webdav_transfers", schema=schema
            )
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
