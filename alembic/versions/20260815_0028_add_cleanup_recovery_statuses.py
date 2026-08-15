"""add terminal cleanup cancellation and recovery statuses

Revision ID: 20260815_0028
Revises: 20260815_0027
Create Date: 2026-08-15 17:20:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260815_0028"
down_revision = "20260815_0027"
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


def _table() -> str:
    return f'"{_schema()}"."document_cleanup_queue"'


def upgrade() -> None:
    queue = _table()
    op.execute(
        f"ALTER TABLE {queue} DROP CONSTRAINT document_cleanup_queue_status_check"
    )
    op.execute(
        f"ALTER TABLE {queue} ADD CONSTRAINT document_cleanup_queue_status_check "
        "CHECK (status IN "
        "('planned', 'running', 'completed', 'failed', 'canceled', 'recovered'))"
    )


def downgrade() -> None:
    queue = _table()
    op.execute(
        f"UPDATE {queue} SET status='completed', phase='completed', "
        "updated_at=CURRENT_TIMESTAMP "
        "WHERE status IN ('canceled', 'recovered')"
    )
    op.execute(
        f"ALTER TABLE {queue} DROP CONSTRAINT document_cleanup_queue_status_check"
    )
    op.execute(
        f"ALTER TABLE {queue} ADD CONSTRAINT document_cleanup_queue_status_check "
        "CHECK (status IN ('planned', 'running', 'completed', 'failed'))"
    )
