"""add run progress and Shayan S3 transfer state

Revision ID: 20260731_0011
Revises: 20260327_0010
Create Date: 2026-07-31 12:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260731_0011"
down_revision = "20260327_0010"
branch_labels = None
depends_on = None

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _schema() -> str:
    try:
        context = op.get_context()
        cfg = context.config if context is not None else None
        configured = (
            str(cfg.get_main_option("manzara_db_schema") or "").strip() if cfg else ""
        )
        value = (
            configured
            or str(os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip()
        )
    except Exception:
        value = str(os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip()
    value = value or "monocorpus"
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
        ADD COLUMN IF NOT EXISTS progress_json TEXT NOT NULL DEFAULT '{{}}'
        """
    )
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_table("shayan_s3_transfers")} (
            source_path TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            source_md5 TEXT NOT NULL,
            source_size BIGINT NOT NULL,
            target_bucket TEXT NOT NULL,
            target_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            error_text TEXT,
            discovered_at TEXT NOT NULL,
            last_attempt_at TEXT,
            moved_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CONSTRAINT ck_shayan_s3_transfer_status
                CHECK (status IN ('pending', 'transferring', 'uploaded', 'moved', 'failed'))
        )
        """
    )
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {_qident("idx_shayan_s3_transfers_status")}
        ON {_table("shayan_s3_transfers")} (status, updated_at, source_path)
        """
    )
    op.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {_qident("idx_shayan_s3_transfers_target")}
        ON {_table("shayan_s3_transfers")} (target_bucket, target_key)
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {_table('shayan_s3_transfers')}")
    op.execute(
        f"""
        ALTER TABLE {_table("runs")}
        DROP COLUMN IF EXISTS progress_json
        """
    )
