"""Persist source-byte-verified MIME for non-PDF extraction.

Revision ID: 20260929_0053
Revises: 20260921_0052
"""

from __future__ import annotations

import os
import re

from alembic import op

revision = "20260929_0053"
down_revision = "20260921_0052"
branch_labels = None
depends_on = None
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _table() -> str:
    schema = str(
        op.get_context().config.get_main_option("manzara_db_schema")
        or os.environ.get("MANZARA_DB_SCHEMA")
        or "monocorpus"
    ).strip()
    if not _SCHEMA_RE.fullmatch(schema):
        raise ValueError(f"Invalid schema name: {schema!r}")
    return f'"{schema}"."library_non_pdf_extraction_state"'


def upgrade() -> None:
    op.execute(f"ALTER TABLE {_table()} ADD COLUMN verified_source_mime TEXT")


def downgrade() -> None:
    op.execute(f"ALTER TABLE {_table()} DROP COLUMN verified_source_mime")
