"""Current durable catalog baseline; earlier upgrade history is retired.

Revision ID: 20261008_0062
Revises: None
"""

from pathlib import Path

from alembic import op
from sqlalchemy import DDL

revision = "20261008_0062"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts["catalog_schema"]
    path = Path(__file__).resolve().parents[1] / "sql" / "baseline_0062.sql"
    sql = path.read_text(encoding="utf-8")
    # DDL avoids treating PostgreSQL JSON literals as SQLAlchemy bind parameters.
    # Escape DDL's percent interpolation, including PL/pgSQL format strings.
    op.execute(DDL(sql.replace("__CATALOG_SCHEMA__", schema).replace("%", "%%")))


def downgrade():
    raise RuntimeError("Baseline rollback requires an explicitly planned backup recovery")
