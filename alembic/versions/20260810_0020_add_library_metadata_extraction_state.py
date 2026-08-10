"""add resumable Library metadata extraction state

Revision ID: 20260810_0020
Revises: 20260808_0019
Create Date: 2026-08-10 12:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260810_0020"
down_revision = "20260808_0019"
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
    table = _table("library_metadata_extraction_state")
    op.execute(
        f"""
        CREATE TABLE {table} (
            md5 TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'partial'
                CHECK (status IN ('partial', 'terminal')),
            attempts_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            model_pool_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            last_run_id BIGINT,
            terminal_reason TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        f"CREATE INDEX idx_library_metadata_extraction_state_status "
        f"ON {table} (status, updated_at)"
    )


def downgrade() -> None:
    op.execute(
        f"DROP TABLE IF EXISTS {_table('library_metadata_extraction_state')}"
    )
