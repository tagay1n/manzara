"""Remove owner-retired application audit and legacy event storage.

Revision ID: 20261011_0065
Revises: 20261011_0064
"""

from pathlib import Path

from alembic import op
from sqlalchemy import DDL

revision = "20261011_0065"
down_revision = "20261011_0064"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts["catalog_schema"]
    path = Path(__file__).resolve().parents[1] / "sql" / "remove_audit_history_0065.sql"
    sql = path.read_text(encoding="utf-8")
    op.execute(DDL(sql.replace("__CATALOG_SCHEMA__", schema).replace("%", "%%")))


def downgrade():
    raise RuntimeError("Restoring retired audit storage requires explicit owner review and a backup")
