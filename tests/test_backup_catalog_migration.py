from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def test_existing_catalog_rows_are_migrated_to_backup(prepared_test_schema) -> None:
    database_url, _prepared_schema = prepared_test_schema
    schema = f"manzara_backup_catalog_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    try:
        command.upgrade(config, "20260812_0021")
        with engine.begin() as conn:
            conn.execute(
                text(
                    f'''INSERT INTO "{schema}".panel_definitions
                    (panel_id,title,created_at,updated_at) VALUES
                    ('shayan','Shayan Console','now','now'),
                    ('maintenance','Maintenance','now','now')'''
                )
            )
            for task_id, title, panel_id in (
                ("shayan.transfer_yadisk_webdav", "Copy Yandex Disk videos to Nextcloud", "shayan"),
                ("maintenance.pgbackrest_backup_full", "Postgres full backup", "maintenance"),
                ("maintenance.pgbackrest_backup_incr", "Postgres incremental backup", "maintenance"),
            ):
                conn.execute(
                    text(
                        f'''INSERT INTO "{schema}".task_definitions
                        (task_id,panel_id,title,task_type,icon_idle,icon_running,command_json,cwd,created_at,updated_at)
                        VALUES (:task_id,:panel_id,:title,'test','Circle','Square','{{}}','/tmp','now','now')'''
                    ),
                    {"task_id": task_id, "panel_id": panel_id, "title": title},
                )
            for workflow_id in (
                "maintenance.pgbackrest_full_weekly",
                "maintenance.pgbackrest_incr_3h",
            ):
                conn.execute(
                    text(
                        f'''INSERT INTO "{schema}".workflows
                        (workflow_id,panel_id,title,description,enabled,created_at,updated_at)
                        VALUES (:workflow_id,'maintenance','Backup','',1,'now','now')'''
                    ),
                    {"workflow_id": workflow_id},
                )

        command.upgrade(config, "head")

        with engine.begin() as conn:
            panels = dict(
                conn.execute(
                    text(f'SELECT panel_id,title FROM "{schema}".panel_definitions')
                ).all()
            )
            tasks = {
                row.task_id: (row.panel_id, row.title)
                for row in conn.execute(
                    text(f'SELECT task_id,panel_id,title FROM "{schema}".task_definitions')
                )
            }
            workflow_panels = {
                row.panel_id
                for row in conn.execute(
                    text(f'SELECT panel_id FROM "{schema}".workflows')
                )
            }

        assert panels["shayan"] == "Shayan"
        assert panels["backup"] == "Backup"
        assert tasks["shayan.transfer_yadisk_webdav"][1] == "Migrate to Hetzner"
        assert tasks["maintenance.pgbackrest_backup_full"] == ("backup", "Full backup")
        assert tasks["maintenance.pgbackrest_backup_incr"] == ("backup", "Incremental backup")
        assert workflow_panels == {"backup"}
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
