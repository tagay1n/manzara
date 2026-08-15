"""rename maintenance sync tasks

Revision ID: 20260815_0025
Revises: 20260812_0024
Create Date: 2026-08-15 12:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260815_0025"
down_revision = "20260812_0024"
branch_labels = None
depends_on = None

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


def _rename(task_id: str, title: str) -> None:
    escaped_task_id = task_id.replace("'", "''")
    escaped_title = title.replace("'", "''")
    op.execute(
        f"UPDATE {_table('task_definitions')} "
        f"SET title = '{escaped_title}', updated_at = CURRENT_TIMESTAMP::text "
        f"WHERE task_id = '{escaped_task_id}'"
    )


def upgrade() -> None:
    _rename("maintenance.monocorpus_sync", "Sync")
    _rename("maintenance.sync_documents_s3", "Migrate to Backblaze S3")


def downgrade() -> None:
    _rename("maintenance.monocorpus_sync", "Monocorpus sync")
    _rename("maintenance.sync_documents_s3", "Sync documents to S3")
