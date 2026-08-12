"""add expiring Gemini blackout override

Revision ID: 20260812_0021
Revises: 20260810_0020
Create Date: 2026-08-12 11:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260812_0021"
down_revision = "20260810_0020"
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


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {_table('gemini_runtime_control')} "
        "ADD COLUMN blackout_override_until TEXT"
    )


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE {_table('gemini_runtime_control')} "
        "DROP COLUMN IF EXISTS blackout_override_until"
    )
