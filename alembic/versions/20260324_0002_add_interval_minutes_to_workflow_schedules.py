"""add interval_minutes to workflow_schedules

Revision ID: 20260324_0002
Revises: 20260324_0001
Create Date: 2026-03-24 22:30:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260324_0002"
down_revision = "20260324_0001"
branch_labels = None
depends_on = None

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _schema() -> str:
    try:
        context = op.get_context()
        cfg = context.config if context is not None else None
        cfg_schema = str(cfg.get_main_option("manzara_db_schema") or "").strip() if cfg else ""
        if cfg_schema:
            value = cfg_schema
        else:
            value = str(os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip()
    except Exception:
        value = str(os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip()

    if not value:
        value = "monocorpus"
    if not _SCHEMA_RE.match(value):
        raise ValueError(f"Invalid schema name: {value!r}")
    return value


def _qident(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _table(name: str) -> str:
    return f"{_qident(_schema())}.{_qident(name)}"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {_table('workflow_schedules')} "
        "ADD COLUMN IF NOT EXISTS interval_minutes BIGINT"
    )


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE {_table('workflow_schedules')} "
        "DROP COLUMN IF EXISTS interval_minutes"
    )

