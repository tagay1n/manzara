"""Apply file privacy to legacy library inclusion without excluding publications.

Revision ID: 20261011_0064
Revises: 20261011_0063
"""

from pathlib import Path

from alembic import op
from sqlalchemy import DDL

revision = "20261011_0064"
down_revision = "20261011_0063"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts["catalog_schema"]
    path = Path(__file__).resolve().parents[1] / "sql" / "document_library_privacy_0064.sql"
    sql = path.read_text(encoding="utf-8")
    op.execute(DDL(sql.replace("__CATALOG_SCHEMA__", schema).replace("%", "%%")))


def downgrade():
    raise RuntimeError("Restoring restricted-file library inclusion requires explicit owner review")
