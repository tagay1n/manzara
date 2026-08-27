"""Scheduler/workflow persistence is removed in favor of the conveyor."""

from __future__ import annotations

from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_alembic_head_drops_legacy_workflow_scheduler_tables(
    prepared_test_schema,
) -> None:
    database_url, _prepared_schema = prepared_test_schema
    schema = f"manzara_remove_scheduler_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    try:
        command.upgrade(config, "20260826_0035")
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".workflows (
                        workflow_id, panel_id, title, description, enabled,
                        created_at, updated_at
                    ) VALUES ('legacy', 'maintenance', 'Legacy', '', 1, 'now', 'now');
                    INSERT INTO "{schema}".workflow_steps (
                        workflow_id, step_order, step_type, condition_json,
                        created_at, updated_at
                    ) VALUES ('legacy', 1, 'task', '{{}}', 'now', 'now');
                    INSERT INTO "{schema}".workflow_schedules (
                        schedule_id, workflow_id, schedule_type, day_of_week,
                        time_of_day, timezone, enabled, overlap_policy,
                        catchup_policy, created_at, updated_at
                    ) VALUES (
                        'legacy.weekly', 'legacy', 'weekly', 1, '03:00', 'UTC',
                        1, 'skip', 'once', 'now', 'now'
                    );
                    INSERT INTO "{schema}".workflow_runs (
                        workflow_id, schedule_id, trigger_source, status,
                        started_at, context_json
                    ) VALUES (
                        'legacy', 'legacy.weekly', 'schedule', 'completed',
                        'now', '{{}}'
                    );
                    INSERT INTO "{schema}".workflow_step_runs (
                        workflow_run_id, step_order, status, started_at, output_json
                    ) VALUES (1, 1, 'completed', 'now', '{{}}');
                    INSERT INTO "{schema}".events (type, ts, payload_json)
                    VALUES ('schedule.triggered', 'now', '{{}}');
                    INSERT INTO "{schema}".events (type, ts, payload_json)
                    VALUES ('workflow.completed', 'now', '{{}}');
                    INSERT INTO "{schema}".events (type, ts, payload_json)
                    VALUES ('task.completed', 'now', '{{}}')
                    """
                )
            )

        command.upgrade(config, "head")

        inspector = inspect(engine)
        for table in (
            "workflows",
            "workflow_steps",
            "workflow_schedules",
            "workflow_runs",
            "workflow_step_runs",
        ):
            assert not inspector.has_table(table, schema=schema)

        with engine.connect() as conn:
            event_types = conn.execute(
                text(f'SELECT type FROM "{schema}".events ORDER BY event_id')
            ).scalars()
            assert list(event_types) == ["task.completed"]
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
