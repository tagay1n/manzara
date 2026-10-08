"""Enforce catalog relationship identities and row-level domain invariants.

Revision ID: 20261007_0060
Revises: 20261006_0059
"""

import os
from pathlib import Path
import re

from alembic import op


revision = "20261007_0060"
down_revision = "20261006_0059"
branch_labels = None
depends_on = None


def upgrade():
    schema = (op.get_context().config.get_main_option("manzara_db_schema")
              or os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("invalid catalog schema")
    ddl = (Path(__file__).resolve().parents[1] / "sql" / "catalog_0060.sql").read_text()
    op.execute(ddl.replace("__CATALOG_SCHEMA__", schema))


def downgrade():
    raise RuntimeError("Catalog integrity changes require an explicitly reviewed forward recovery.")
