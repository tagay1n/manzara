"""Install an explicit coordinated cutover, dormant until a reviewed activation.

Revision ID: 20261005_0057
Revises: 20261005_0056
"""

import os
from pathlib import Path
import re

from alembic import op

revision = "20261005_0057"
down_revision = "20261005_0056"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().config.get_main_option("manzara_db_schema") or os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("invalid catalog schema")
    ddl = (Path(__file__).resolve().parents[1] / "sql" / "catalog_task_adapters_0057.sql").read_text().replace("__CATALOG_SCHEMA__", schema)
    # Alembic owns every function and trigger. Activation supplies only the
    # reviewed source schema; an ordinary startup upgrade never cuts over.
    op.execute(f'''CREATE FUNCTION "{schema}".catalog_activate_task_adapters(dataset_schema TEXT)
        RETURNS VOID LANGUAGE plpgsql AS $installer$
        BEGIN
          IF dataset_schema !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
            RAISE EXCEPTION 'invalid dataset schema';
          END IF;
          IF NOT EXISTS(SELECT 1 FROM "{schema}".catalog_imports WHERE state='verified') THEN
            RAISE EXCEPTION 'reviewed catalog import required';
          END IF;
          EXECUTE replace($adapter_ddl${ddl}$adapter_ddl$, '__DATASET_SCHEMA__', dataset_schema);
        END $installer$''')


def downgrade():
    raise RuntimeError("An activated catalog requires explicit backup recovery.")
