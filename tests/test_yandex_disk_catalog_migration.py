"""Alembic coverage for the Yandex Disk catalog rename and task move."""

from __future__ import annotations

from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def test_existing_maintenance_catalog_and_cleanup_history_are_migrated(
    prepared_test_schema: tuple[str, str],
) -> None:
    database_url, _prepared_schema = prepared_test_schema
    schema = f"manzara_yandex_catalog_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    task_id = "library.prepare_document_cleanup"
    try:
        command.upgrade(config, "20260815_0026")
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".panel_definitions
                        (panel_id, title, created_at, updated_at)
                    VALUES ('maintenance', 'Maintenance', 'now', 'now')
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
                        :task_id, 'library', 'Prepare document cleanup', 'scan',
                        'ListFilter', 'Square', '{{}}', '/tmp', '{{}}', 'now', 'now'
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
                    ) VALUES (:task_id, 'library', 'completed', 'now', 'now', 'now')
                    RETURNING run_id
                    """
                ),
                {"task_id": task_id},
            ).scalar_one()
            conn.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".events
                        (type, task_id, run_id, panel_id, ts, payload_json)
                    VALUES ('task.completed', :task_id, :run_id, 'library', 'now', '{{}}')
                    """
                ),
                {"task_id": task_id, "run_id": run_id},
            )

        command.upgrade(config, "head")

        with engine.connect() as conn:
            assert conn.execute(
                text(
                    f'SELECT title FROM "{schema}".panel_definitions '
                    "WHERE panel_id = 'maintenance'"
                )
            ).scalar_one() == "Yandex disk"
            task = conn.execute(
                text(
                    f'SELECT panel_id, title FROM "{schema}".task_definitions '
                    "WHERE task_id = :task_id"
                ),
                {"task_id": task_id},
            ).one()
            assert tuple(task) == ("maintenance", "Cleanup plan")
            assert conn.execute(
                text(f'SELECT panel_id FROM "{schema}".runs WHERE run_id = :run_id'),
                {"run_id": run_id},
            ).scalar_one() == "maintenance"
            assert conn.execute(
                text(f'SELECT panel_id FROM "{schema}".events WHERE run_id = :run_id'),
                {"run_id": run_id},
            ).scalar_one() == "maintenance"
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
