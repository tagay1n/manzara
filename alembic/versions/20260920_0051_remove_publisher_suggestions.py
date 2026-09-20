"""remove retired publisher suggestions

Revision ID: 20260920_0051
Revises: 20260919_0050
Create Date: 2026-09-20 15:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op

revision = "20260920_0051"
down_revision = "20260919_0050"
branch_labels = None
depends_on = None
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _table(name: str) -> str:
    schema = str(op.get_context().config.get_main_option("manzara_db_schema") or os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip()
    if not _SCHEMA_RE.fullmatch(schema):
        raise ValueError(f"Invalid schema name: {schema!r}")
    return f'"{schema}"."{name}"'


def upgrade() -> None:
    op.execute(f"DELETE FROM {_table('normalization_suggestions')} WHERE entity_type = 'publisher'")


def downgrade() -> None:
    raise RuntimeError("Publisher suggestion data was intentionally removed.")
