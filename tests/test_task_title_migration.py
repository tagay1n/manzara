from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def test_existing_maintenance_task_titles_are_renamed(prepared_test_schema) -> None:
    database_url, _prepared_schema = prepared_test_schema
    schema = f"manzara_task_titles_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    try:
        command.upgrade(config, "20260812_0024")
        with engine.begin() as conn:
            for task_id, title in (
                ("maintenance.monocorpus_sync", "Monocorpus sync"),
                ("maintenance.sync_documents_s3", "Sync documents to S3"),
            ):
                conn.execute(
                    text(
                        f'''INSERT INTO "{schema}".task_definitions
                        (task_id,panel_id,title,task_type,icon_idle,icon_running,
                         command_json,cwd,created_at,updated_at)
                        VALUES (:task_id,'maintenance',:title,'test','Circle','Square',
                                '{{}}','/tmp','now','now')'''
                    ),
                    {"task_id": task_id, "title": title},
                )

        command.upgrade(config, "head")

        with engine.connect() as conn:
            titles = dict(
                conn.execute(
                    text(
                        f'SELECT task_id,title FROM "{schema}".task_definitions '
                        "WHERE task_id IN "
                        "('maintenance.monocorpus_sync','maintenance.sync_documents_s3')"
                    )
                ).all()
            )

        assert titles == {
            "maintenance.monocorpus_sync": "Sync",
            "maintenance.sync_documents_s3": "Upload to Backblaze S3",
        }
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
