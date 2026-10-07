"""Prepare lossless catalog retirement; ordinary upgrades never drop source tables.

Revision ID: 20261006_0059
Revises: 20261005_0058
"""

import os
from pathlib import Path
import re

from alembic import op

revision = "20261006_0059"
down_revision = "20261005_0058"
branch_labels = None
depends_on = None


def upgrade():
    schema = (op.get_context().config.get_main_option("manzara_db_schema")
              or os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("invalid catalog schema")
    sql_root = Path(__file__).resolve().parents[1] / "sql"
    op.execute((sql_root / "catalog_0059.sql").read_text().replace("__CATALOG_SCHEMA__", schema))
    ddl = (sql_root / "catalog_lean_adapters_0059.sql").read_text().replace("__CATALOG_SCHEMA__", schema)
    op.execute(f'''CREATE FUNCTION "{schema}".catalog_install_lean_adapters(dataset_schema TEXT)
        RETURNS VOID LANGUAGE plpgsql AS $installer$
        BEGIN
          IF dataset_schema !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
            RAISE EXCEPTION 'invalid dataset schema';
          END IF;
          IF NOT EXISTS(SELECT 1 FROM "{schema}".catalog_imports
              WHERE state='verified' AND manifest->>'evidence_mode'='essential') THEN
            RAISE EXCEPTION 'verified essential-evidence import required';
          END IF;
          EXECUTE replace($lean_ddl${ddl}$lean_ddl$, '__DATASET_SCHEMA__', dataset_schema);
        END $installer$''')


def downgrade():
    raise RuntimeError("Retired catalog tables require explicit backup recovery.")
