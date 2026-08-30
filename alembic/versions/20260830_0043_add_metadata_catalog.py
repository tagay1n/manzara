"""add dedicated Metadata task catalog

Revision ID: 20260830_0043
Revises: 20260830_0042
Create Date: 2026-08-30 16:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260830_0043"
down_revision = "20260830_0042"
branch_labels = None
depends_on = None

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TASK_IDS = (
    "maintenance.monocorpus_meta_evaluate",
    "library.metadata_extract",
    "library.metadata_validate",
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


def _task_id_list() -> str:
    return ", ".join(f"'{task_id}'" for task_id in _TASK_IDS)


def upgrade() -> None:
    now = "CURRENT_TIMESTAMP::text"
    op.execute(
        f"""
        INSERT INTO {_table('panel_definitions')}
            (panel_id, title, created_at, updated_at)
        VALUES ('metadata', 'Metadata', {now}, {now})
        ON CONFLICT (panel_id) DO NOTHING
        """
    )
    op.execute(
        f"""
        UPDATE {_table('task_definitions')}
        SET panel_id = 'metadata',
            title = CASE task_id
                WHEN 'maintenance.monocorpus_meta_evaluate' THEN 'Evaluate metadata'
                ELSE title
            END,
            updated_at = {now}
        WHERE task_id IN ({_task_id_list()})
        """
    )
    for table in ("runs", "events"):
        op.execute(
            f"UPDATE {_table(table)} SET panel_id = 'metadata' "
            f"WHERE task_id IN ({_task_id_list()})"
        )


def downgrade() -> None:
    now = "CURRENT_TIMESTAMP::text"
    for table in ("events", "runs"):
        op.execute(
            f"UPDATE {_table(table)} SET panel_id = 'library' "
            f"WHERE task_id IN ({_task_id_list()})"
        )
    op.execute(
        f"""
        UPDATE {_table('task_definitions')}
        SET panel_id = 'library',
            title = CASE task_id
                WHEN 'maintenance.monocorpus_meta_evaluate' THEN 'Monocorpus meta evaluate'
                ELSE title
            END,
            updated_at = {now}
        WHERE task_id IN ({_task_id_list()})
        """
    )
    op.execute(f"DELETE FROM {_table('panel_definitions')} WHERE panel_id = 'metadata'")
