"""Repair member identity and search indexes in the first deployed catalog.

Revision ID: 20261005_0058
Revises: 20261005_0057
"""

import os
import re

from alembic import op

revision = "20261005_0058"
down_revision = "20261005_0057"
branch_labels = None
depends_on = None


def upgrade():
    schema = (op.get_context().config.get_main_option("manzara_db_schema")
              or os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("invalid catalog schema")
    # Early installations of 0056 omitted this column. Keep that installed
    # revision intact and assign IDs to existing members without losing rows.
    op.execute(f'''DO $repair$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_attribute
            WHERE attrelid='"{schema}".catalog_proposal_members'::regclass
              AND attname='member_id' AND NOT attisdropped
          ) THEN
            ALTER TABLE "{schema}".catalog_proposal_members
              ADD COLUMN member_id BIGSERIAL PRIMARY KEY;
          END IF;
        END $repair$''')
    op.execute(f'''CREATE INDEX IF NOT EXISTS idx_catalog_contributions_name_role
        ON "{schema}".catalog_contributions(name_id,role)''')
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for table, columns in {
        "publications": ("name", "description", "edition"),
        "entities": ("display_name",), "names": ("raw_name",),
        "collections": ("title",), "classification_nodes": ("label_en", "label_tt"),
    }.items():
        for column in columns:
            op.execute(f'''CREATE INDEX IF NOT EXISTS "idx_catalog_{table}_{column}_trgm"
                ON "{schema}"."catalog_{table}" USING gin ("{column}" gin_trgm_ops)''')


def downgrade():
    raise RuntimeError("Catalog proposal-member identities require explicit recovery.")
