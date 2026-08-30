"""Alembic coverage for the dedicated Metadata task catalog."""

from __future__ import annotations

from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def test_existing_metadata_tasks_and_history_are_moved(prepared_test_schema) -> None:
    database_url, _prepared_schema = prepared_test_schema
    schema = f"manzara_metadata_catalog_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    task_rows = (
        ("maintenance.monocorpus_meta_evaluate", "library", "Monocorpus meta evaluate"),
        ("library.metadata_extract", "library", "Extract metadata"),
        ("library.metadata_validate", "library", "Validate metadata"),
    )
    try:
        command.upgrade(config, "20260830_0042")
        with engine.begin() as conn:
            for task_id, panel_id, title in task_rows:
                conn.execute(
                    text(
                        f'''INSERT INTO "{schema}".task_definitions
                        (task_id,panel_id,title,task_type,icon_idle,icon_running,
                         command_json,cwd,created_at,updated_at)
                        VALUES (:task_id,:panel_id,:title,'metadata','Circle','Square',
                                '{{}}','/tmp','now','now')'''
                    ),
                    {"task_id": task_id, "panel_id": panel_id, "title": title},
                )
                run_id = conn.execute(
                    text(
                        f'''INSERT INTO "{schema}".runs
                        (task_id,panel_id,status,started_at,created_at,updated_at)
                        VALUES (:task_id,:panel_id,'completed','now','now','now')
                        RETURNING run_id'''
                    ),
                    {"task_id": task_id, "panel_id": panel_id},
                ).scalar_one()
                conn.execute(
                    text(
                        f'''INSERT INTO "{schema}".events
                        (type,task_id,run_id,panel_id,ts,payload_json)
                        VALUES ('task.completed',:task_id,:run_id,:panel_id,'now','{{}}')'''
                    ),
                    {"task_id": task_id, "run_id": run_id, "panel_id": panel_id},
                )

        command.upgrade(config, "head")

        with engine.connect() as conn:
            assert conn.execute(
                text(
                    f'''SELECT title FROM "{schema}".panel_definitions
                    WHERE panel_id='metadata' '''
                )
            ).scalar_one() == "Metadata"
            definitions = conn.execute(
                text(
                    f'''SELECT task_id,panel_id,title FROM "{schema}".task_definitions
                    WHERE task_id IN (
                        'maintenance.monocorpus_meta_evaluate',
                        'library.metadata_extract',
                        'library.metadata_validate'
                    ) ORDER BY task_id'''
                )
            ).all()
            assert {row[0]: (row[1], row[2]) for row in definitions} == {
                "maintenance.monocorpus_meta_evaluate": ("metadata", "Evaluate metadata"),
                "library.metadata_extract": ("metadata", "Extract metadata"),
                "library.metadata_validate": ("metadata", "Validate metadata"),
            }
            for table in ("runs", "events"):
                panels = conn.execute(
                    text(
                        f'''SELECT DISTINCT panel_id FROM "{schema}".{table}
                        WHERE task_id IN (
                            'maintenance.monocorpus_meta_evaluate',
                            'library.metadata_extract',
                            'library.metadata_validate'
                        )'''
                    )
                ).scalars().all()
                assert panels == ["metadata"]
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
