"""remove retired content migration checkpoints

Revision ID: 20260919_0050
Revises: 20260919_0049
Create Date: 2026-09-19 14:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260919_0050"
down_revision = "20260919_0049"
branch_labels = None
depends_on = None
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _schema() -> str:
    value = str(
        op.get_context().config.get_main_option("manzara_db_schema")
        or os.environ.get("MANZARA_DB_SCHEMA")
        or "monocorpus"
    ).strip()
    if not _SCHEMA_RE.fullmatch(value):
        raise ValueError(f"Invalid schema name: {value!r}")
    return value


def _table(name: str) -> str:
    return f'"{_schema()}"."{name}"'


def upgrade() -> None:
    op.execute(
        f"DROP TABLE IF EXISTS {_table('maintenance_content_match_cleanup')}"
    )
    op.execute(
        f"DROP TABLE IF EXISTS {_table('maintenance_content_migration_images')}"
    )
    op.execute(f"DROP TABLE IF EXISTS {_table('maintenance_content_migration')}")


def downgrade() -> None:
    raise RuntimeError(
        "Retired content migration checkpoints contain disposable task state; "
        "restore them only by upgrading from the previous revision."
    )
