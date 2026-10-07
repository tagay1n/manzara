"""Install shared normalized catalog tables; import/cutover is explicitly offline.

Revision ID: 20261005_0056
Revises: 20261001_0055
"""

import os
from pathlib import Path
import re

from alembic import op

revision = "20261005_0056"
down_revision = "20261001_0055"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().config.get_main_option("manzara_db_schema") or os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("Invalid catalog schema")
    ddl = (Path(__file__).resolve().parents[1] / "sql" / "catalog_0056.sql").read_text()
    op.execute(ddl.replace("__CATALOG_SCHEMA__", schema))
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for table, columns in {
        "publications": ("name", "description", "edition"),
        "entities": ("display_name",), "names": ("raw_name",),
        "collections": ("title",), "classification_nodes": ("label_en", "label_tt"),
    }.items():
        for column in columns:
            op.execute(f'CREATE INDEX "idx_catalog_{table}_{column}_trgm" '
                       f'ON "{schema}"."catalog_{table}" USING gin ("{column}" gin_trgm_ops)')


def downgrade():
    raise RuntimeError("Catalog editions, identity decisions and revisions require explicit recovery.")
