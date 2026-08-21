"""version Library metadata extraction attempts and service deferrals

Revision ID: 20260820_0031
Revises: 20260820_0030
Create Date: 2026-08-20 18:30:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260820_0031"
down_revision = "20260820_0030"
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
    op.execute(f"ALTER TABLE {table} ADD COLUMN prompt_version TEXT")
    op.execute(f"ALTER TABLE {table} ADD COLUMN retry_after TIMESTAMPTZ")
    op.execute(
        f"ALTER TABLE {table} ADD COLUMN operational_failure_count "
        "INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(f"ALTER TABLE {table} ADD COLUMN last_operational_error TEXT")
    op.execute(
        "CREATE INDEX idx_library_metadata_extraction_state_retry "
        f"ON {table} (status, prompt_version, retry_after, updated_at)"
    )


def downgrade() -> None:
    table = _table("library_metadata_extraction_state")
    op.execute("DROP INDEX IF EXISTS " f'"{_schema()}".idx_library_metadata_extraction_state_retry')
    op.execute(f"ALTER TABLE {table} DROP COLUMN last_operational_error")
    op.execute(f"ALTER TABLE {table} DROP COLUMN operational_failure_count")
    op.execute(f"ALTER TABLE {table} DROP COLUMN retry_after")
    op.execute(f"ALTER TABLE {table} DROP COLUMN prompt_version")
