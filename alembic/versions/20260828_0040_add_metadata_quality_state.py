"""add versioned Library metadata quality state

Revision ID: 20260828_0040
Revises: 20260828_0039
Create Date: 2026-08-28 18:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260828_0040"
down_revision = "20260828_0039"
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
        f"ALTER TABLE {_table('library_metadata_evaluation_state')} "
        "ADD COLUMN prompt_version TEXT"
    )
    table = _table("library_metadata_quality_state")
    op.execute(
        f"""
        CREATE TABLE {table} (
            md5 TEXT PRIMARY KEY,
            contract_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'invalid'
                CHECK (status IN ('invalid', 'resolved')),
            issues_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            last_run_id BIGINT,
            detected_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        f"CREATE INDEX idx_library_metadata_quality_state_status "
        f"ON {table} (status, contract_version, updated_at)"
    )


def downgrade() -> None:
    op.execute(
        f"DROP TABLE IF EXISTS {_table('library_metadata_quality_state')}"
    )
    op.execute(
        f"ALTER TABLE {_table('library_metadata_evaluation_state')} "
        "DROP COLUMN IF EXISTS prompt_version"
    )
