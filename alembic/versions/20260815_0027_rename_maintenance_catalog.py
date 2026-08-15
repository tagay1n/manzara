"""rename Maintenance catalog and move cleanup planning task

Revision ID: 20260815_0027
Revises: 20260815_0026
Create Date: 2026-08-15 17:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260815_0027"
down_revision = "20260815_0026"
branch_labels = None
depends_on = None

_TASK_ID = "library.prepare_document_cleanup"
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


def upgrade() -> None:
    now = "CURRENT_TIMESTAMP::text"
    op.execute(
        f"""
        INSERT INTO {_table('panel_definitions')} (
            panel_id, title, created_at, updated_at
        ) VALUES ('maintenance', 'Yandex disk', {now}, {now})
        ON CONFLICT (panel_id) DO UPDATE SET
            title = EXCLUDED.title,
            updated_at = EXCLUDED.updated_at
        """
    )
    op.execute(
        f"""
        UPDATE {_table('task_definitions')}
        SET panel_id = 'maintenance', title = 'Cleanup plan', updated_at = {now}
        WHERE task_id = '{_TASK_ID}'
        """
    )
    op.execute(
        f"UPDATE {_table('runs')} SET panel_id = 'maintenance' "
        f"WHERE task_id = '{_TASK_ID}'"
    )
    op.execute(
        f"UPDATE {_table('events')} SET panel_id = 'maintenance' "
        f"WHERE task_id = '{_TASK_ID}'"
    )


def downgrade() -> None:
    now = "CURRENT_TIMESTAMP::text"
    op.execute(
        f"UPDATE {_table('events')} SET panel_id = 'library' "
        f"WHERE task_id = '{_TASK_ID}'"
    )
    op.execute(
        f"UPDATE {_table('runs')} SET panel_id = 'library' "
        f"WHERE task_id = '{_TASK_ID}'"
    )
    op.execute(
        f"""
        UPDATE {_table('task_definitions')}
        SET panel_id = 'library', title = 'Prepare document cleanup', updated_at = {now}
        WHERE task_id = '{_TASK_ID}'
        """
    )
    op.execute(
        f"""
        UPDATE {_table('panel_definitions')}
        SET title = 'Maintenance', updated_at = {now}
        WHERE panel_id = 'maintenance'
        """
    )
