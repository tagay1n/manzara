"""Ensure publisher review edits are available on deployed proposal tables.

Revision ID: 20261001_0055
Revises: 20261001_0054
"""

from __future__ import annotations

import os
import re

from alembic import op

revision = "20261001_0055"
down_revision = "20261001_0054"
branch_labels = None
depends_on = None


def upgrade():
    schema = (
        op.get_context().config.get_main_option("manzara_db_schema")
        or os.environ.get("MANZARA_DB_SCHEMA")
        or "monocorpus"
    )
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("Invalid schema name")
    # Earlier deployments of 0054 lack the column; fresh installs may have it.
    op.execute(
        f'ALTER TABLE "{schema}"."publisher_merge_proposals" '
        "ADD COLUMN IF NOT EXISTS review_edit JSONB"
    )


def downgrade():
    raise RuntimeError("Publisher owner review edits are durable workflow data.")
