"""add runs summary_json

Revision ID: 20260325_0006
Revises: 20260325_0005
Create Date: 2026-03-25 16:10:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260325_0006"
down_revision = "20260325_0005"
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
        f"""
        ALTER TABLE {_table("runs")}
        ADD COLUMN IF NOT EXISTS summary_json TEXT NOT NULL DEFAULT '{{}}'
        """
    )
    op.execute(
        f"""
        UPDATE {_table("runs")}
        SET summary_json = '{{}}'
        WHERE summary_json IS NULL
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE {_table("runs")}
        DROP COLUMN IF EXISTS summary_json
        """
    )

