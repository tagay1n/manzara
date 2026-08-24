"""simplify Library book preview checkpoints

Revision ID: 20260824_0033
Revises: 20260821_0032
Create Date: 2026-08-24 12:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260824_0033"
down_revision = "20260821_0032"
branch_labels = None
depends_on = None

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _schema() -> str:
    context = op.get_context()
    configured = str(context.config.get_main_option("manzara_db_schema") or "").strip()
    value = configured or str(os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip()
    value = value or "monocorpus"
    if not _SCHEMA_RE.fullmatch(value):
        raise ValueError(f"Invalid schema name: {value!r}")
    return value


def _table() -> str:
    schema = _schema().replace('"', '""')
    return f'"{schema}"."library_book_previews"'


def upgrade() -> None:
    op.execute(f"ALTER TABLE {_table()} DROP COLUMN manifest_json")


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE {_table()} ADD COLUMN manifest_json JSONB "
        "NOT NULL DEFAULT '{}'::jsonb"
    )
