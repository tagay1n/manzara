"""add shayan manifest yadisk state columns

Revision ID: 20260326_0009
Revises: 20260326_0008
Create Date: 2026-03-26 18:10:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260326_0009"
down_revision = "20260326_0008"
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
    table = _table("shayan_manifest_entries")
    op.execute(
        f"""
        ALTER TABLE {table}
        ADD COLUMN IF NOT EXISTS yadisk_status TEXT NOT NULL DEFAULT 'pending'
        """
    )
    op.execute(
        f"""
        ALTER TABLE {table}
        ADD COLUMN IF NOT EXISTS yadisk_remote_path TEXT
        """
    )
    op.execute(
        f"""
        ALTER TABLE {table}
        ADD COLUMN IF NOT EXISTS yadisk_uploaded_payload_hash TEXT
        """
    )
    op.execute(
        f"""
        ALTER TABLE {table}
        ADD COLUMN IF NOT EXISTS yadisk_uploaded_at TEXT
        """
    )
    op.execute(
        f"""
        ALTER TABLE {table}
        ADD COLUMN IF NOT EXISTS yadisk_last_attempt_at TEXT
        """
    )
    op.execute(
        f"""
        ALTER TABLE {table}
        ADD COLUMN IF NOT EXISTS yadisk_last_error TEXT
        """
    )
    op.execute(
        f"""
        UPDATE {table}
        SET yadisk_status = 'pending'
        WHERE COALESCE(yadisk_status, '') = ''
        """
    )
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {_qident("idx_shayan_manifest_entries_yadisk_status")}
        ON {table} (yadisk_status, updated_at DESC)
        """
    )


def downgrade() -> None:
    table = _table("shayan_manifest_entries")
    op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS yadisk_last_error")
    op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS yadisk_last_attempt_at")
    op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS yadisk_uploaded_at")
    op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS yadisk_uploaded_payload_hash")
    op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS yadisk_remote_path")
    op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS yadisk_status")
