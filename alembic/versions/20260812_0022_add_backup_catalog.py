"""add dedicated backup catalog and rename requested definitions

Revision ID: 20260812_0022
Revises: 20260812_0021
Create Date: 2026-08-12 17:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260812_0022"
down_revision = "20260812_0021"
branch_labels = None
depends_on = None

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BACKUP_TASK_IDS = (
    "maintenance.pgbackrest_backup_full",
    "maintenance.pgbackrest_backup_incr",
)


def _schema() -> str:
    configured = str(
        op.get_context().config.get_main_option("manzara_db_schema") or ""
    ).strip()
    value = configured or str(
        os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus"
    ).strip()
    if not _SCHEMA_RE.fullmatch(value):
        raise ValueError(f"Invalid schema name: {value!r}")
    return value


def _table(name: str) -> str:
    return f'"{_schema()}"."{name}"'


def _task_ids_sql() -> str:
    return ", ".join(f"'{task_id}'" for task_id in _BACKUP_TASK_IDS)


def upgrade() -> None:
    now = "CURRENT_TIMESTAMP::text"
    op.execute(
        f"""
        INSERT INTO {_table('panel_definitions')} (
            panel_id, title, created_at, updated_at
        ) VALUES ('backup', 'Backup', {now}, {now})
        ON CONFLICT (panel_id) DO UPDATE SET
            title = EXCLUDED.title,
            updated_at = EXCLUDED.updated_at
        """
    )
    op.execute(
        f"UPDATE {_table('panel_definitions')} "
        f"SET title = 'Shayan', updated_at = {now} WHERE panel_id = 'shayan'"
    )
    op.execute(
        f"UPDATE {_table('task_definitions')} "
        f"SET title = 'Migrate to Hetzner', updated_at = {now} "
        "WHERE task_id = 'shayan.transfer_yadisk_webdav'"
    )
    op.execute(
        f"UPDATE {_table('task_definitions')} "
        f"SET panel_id = 'backup', title = 'Full backup', updated_at = {now} "
        "WHERE task_id = 'maintenance.pgbackrest_backup_full'"
    )
    op.execute(
        f"UPDATE {_table('task_definitions')} "
        f"SET panel_id = 'backup', title = 'Incremental backup', updated_at = {now} "
        "WHERE task_id = 'maintenance.pgbackrest_backup_incr'"
    )
    op.execute(
        f"UPDATE {_table('workflows')} SET panel_id = 'backup', updated_at = {now} "
        "WHERE workflow_id IN ("
        "'maintenance.pgbackrest_full_weekly', "
        "'maintenance.pgbackrest_incr_3h')"
    )
    task_ids = _task_ids_sql()
    op.execute(
        f"UPDATE {_table('runs')} SET panel_id = 'backup' WHERE task_id IN ({task_ids})"
    )
    op.execute(
        f"UPDATE {_table('events')} SET panel_id = 'backup' WHERE task_id IN ({task_ids})"
    )


def downgrade() -> None:
    now = "CURRENT_TIMESTAMP::text"
    task_ids = _task_ids_sql()
    op.execute(
        f"UPDATE {_table('runs')} SET panel_id = 'maintenance' WHERE task_id IN ({task_ids})"
    )
    op.execute(
        f"UPDATE {_table('events')} SET panel_id = 'maintenance' WHERE task_id IN ({task_ids})"
    )
    op.execute(
        f"UPDATE {_table('workflows')} SET panel_id = 'maintenance', updated_at = {now} "
        "WHERE workflow_id IN ("
        "'maintenance.pgbackrest_full_weekly', "
        "'maintenance.pgbackrest_incr_3h')"
    )
    op.execute(
        f"UPDATE {_table('task_definitions')} "
        f"SET panel_id = 'maintenance', title = 'Postgres full backup', updated_at = {now} "
        "WHERE task_id = 'maintenance.pgbackrest_backup_full'"
    )
    op.execute(
        f"UPDATE {_table('task_definitions')} "
        f"SET panel_id = 'maintenance', title = 'Postgres incremental backup', updated_at = {now} "
        "WHERE task_id = 'maintenance.pgbackrest_backup_incr'"
    )
    op.execute(
        f"UPDATE {_table('task_definitions')} "
        f"SET title = 'Copy Yandex Disk videos to Nextcloud', updated_at = {now} "
        "WHERE task_id = 'shayan.transfer_yadisk_webdav'"
    )
    op.execute(
        f"DELETE FROM {_table('panel_definitions')} WHERE panel_id = 'backup'"
    )
