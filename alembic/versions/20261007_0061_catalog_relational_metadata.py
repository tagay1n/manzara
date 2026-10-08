"""Normalize catalog lists and credit groups; PostgreSQL owns derived keys.

Revision ID: 20261007_0061
Revises: 20261007_0060
"""

import os
from pathlib import Path
import re

from alembic import op


revision = "20261007_0061"
down_revision = "20261007_0060"
branch_labels = None
depends_on = None


def _adapter_ddl(sql_root, schema):
    """Retarget the frozen reader envelopes without copying their definitions."""
    ddl = (sql_root / "catalog_lean_adapters_0059.sql").read_text()
    projections = {
        "catalog_publications": "catalog_publication_metadata",
        "catalog_documents": "catalog_document_metadata",
        "catalog_contributions": "catalog_contribution_metadata",
        "catalog_sufficient_modes": "catalog_sufficient_mode_metadata",
        "catalog_references": "catalog_reference_metadata",
    }
    for table, view in projections.items():
        ddl = re.sub(r"\b" + table + r"\b", view, ddl)
    ddl = re.sub(r"CREATE (VIEW|FUNCTION|TRIGGER)\b", r"CREATE OR REPLACE \1", ddl)
    ddl = ddl.replace("lower(e.display_name)", '"__CATALOG_SCHEMA__".catalog_name_key(e.display_name)')
    ddl = ddl.replace("lower(c.title)", '"__CATALOG_SCHEMA__".catalog_title_key(c.title)')
    return ddl.replace("__CATALOG_SCHEMA__", schema)


def upgrade():
    schema = (op.get_context().config.get_main_option("manzara_db_schema")
              or os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("invalid catalog schema")
    sql_root = Path(__file__).resolve().parents[1] / "sql"
    op.execute("""DO $requirements$ BEGIN
      IF current_setting('server_version_num')::integer < 180000
        OR current_setting('server_encoding') <> 'UTF8' THEN
        RAISE EXCEPTION 'catalog normalization requires PostgreSQL 18+ with UTF8';
      END IF;
    END $requirements$""")
    op.execute((sql_root / "catalog_0061.sql").read_text().replace("__CATALOG_SCHEMA__", schema))
    op.execute((sql_root / "catalog_normalization_0061.sql").read_text().replace("__CATALOG_SCHEMA__", schema))
    ddl = _adapter_ddl(sql_root, schema)
    # The active manifest identifies the existing legacy read-view namespace.
    # A fresh empty catalog instead installs its read views during cutover.
    op.execute(f'''DO $readers$
        DECLARE dataset TEXT;
        BEGIN
          FOR dataset IN SELECT DISTINCT manifest->>'dataset_schema'
            FROM "{schema}".catalog_imports WHERE state='active' LOOP
            IF dataset IS NULL OR dataset !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
              RAISE EXCEPTION 'active catalog has no valid dataset schema';
            END IF;
            EXECUTE replace($adapter_ddl${ddl}$adapter_ddl$,'__DATASET_SCHEMA__',dataset);
          END LOOP;
        END $readers$''')
    op.execute(f'''CREATE OR REPLACE FUNCTION "{schema}".catalog_install_lean_adapters(dataset_schema TEXT)
        RETURNS VOID LANGUAGE plpgsql AS $installer$
        BEGIN
          IF dataset_schema IS NULL OR dataset_schema !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
            RAISE EXCEPTION 'invalid dataset schema';
          END IF;
          IF NOT EXISTS(SELECT 1 FROM "{schema}".catalog_imports
              WHERE state='verified' AND manifest->>'evidence_mode'='essential') THEN
            RAISE EXCEPTION 'verified essential-evidence import required';
          END IF;
          EXECUTE replace($adapter_ddl${ddl}$adapter_ddl$,'__DATASET_SCHEMA__',dataset_schema);
        END $installer$''')
    # RESTRICT refuses any additional readers not adapted by this revision.
    # No CASCADE can silently remove third-party schema objects.
    for table, column in (
        ("catalog_publications", "languages"), ("catalog_documents", "access_modes"),
        ("catalog_sufficient_modes", "modes"), ("catalog_references", "urls"),
        ("catalog_contributions", "role_name"),
    ):
        op.execute(f'ALTER TABLE "{schema}"."{table}" DROP COLUMN "{column}" RESTRICT')
    op.execute((sql_root / "catalog_topology_0061.sql").read_text().replace("__CATALOG_SCHEMA__", schema))


def downgrade():
    raise RuntimeError("Relational metadata requires an explicitly reviewed forward recovery.")
