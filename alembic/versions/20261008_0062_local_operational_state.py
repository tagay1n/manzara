"""Retire reproducible caches and duplicate flow tables after local transfer.

Revision ID: 20261008_0062
Revises: 20261007_0061
"""

import os
from pathlib import Path
import re

from alembic import op
from sqlalchemy import text

revision = "20261008_0062"
down_revision = "20261007_0061"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().config.get_main_option("manzara_db_schema") or os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("Invalid catalog schema")
    receipt = op.get_context().config.attributes.get("storage_cutover_receipt")
    if not receipt or receipt.get("revision") != revision:
        raise RuntimeError("Use scripts/reduce_postgres_storage.py --apply: verified local transfer and recovery dump are mandatory")
    conn = op.get_bind()
    for table, manifest in receipt["sources"].items():
        # Sources and keys are a reviewed registry owned by the cutover command.
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise ValueError("Invalid source relation")
        rows = conn.execute(text(f'SELECT count(*) FROM "{schema}"."{table}"')).scalar_one()
        if rows != manifest["rows"]:
            raise RuntimeError("Cutover source count changed: " + table)
    sql = (Path(__file__).resolve().parents[1] / "sql" / "storage_0062.sql").read_text()
    op.execute(sql.replace("__CATALOG_SCHEMA__", schema))
    import json
    conn.execute(text(f'''INSERT INTO "{schema}".catalog_evidence
        (record_kind,record_key,source,payload) VALUES('schema_migration',:key,'storage-cutover',CAST(:payload AS jsonb))'''),
        {"key": revision, "payload": json.dumps(receipt)})


def downgrade():
    raise RuntimeError("Storage retirement requires explicit recovery from the recorded dump; local retries have one owner")
