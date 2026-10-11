"""Add full-text lookup indexes for the read-only catalog administration UI.

Revision ID: 20261011_0063
Revises: 20261008_0062
"""

from alembic import op
from sqlalchemy import DDL

revision = "20261011_0063"
down_revision = "20261008_0062"
branch_labels = None
depends_on = None

# Expressions must match the admin query compiler, including the explicit
# language-neutral configuration and NULL handling. No generated columns,
# document content, or publication data are changed.
INDEXES = (
    ("idx_catalog_publications_name_fts", "catalog_publications", "coalesce(name, '')"),
    ("idx_catalog_publications_edition_fts", "catalog_publications", "coalesce(edition, '')"),
    ("idx_catalog_names_raw_name_fts", "catalog_names", "coalesce(raw_name, '')"),
    ("idx_catalog_entities_display_name_fts", "catalog_entities", "coalesce(display_name, '')"),
    ("idx_catalog_collections_title_fts", "catalog_collections", "coalesce(title, '')"),
    (
        "idx_catalog_classification_nodes_search_fts",
        "catalog_classification_nodes",
        "coalesce(coalesce(ddc, '') || ' ' || coalesce(label_en, '') || ' ' || coalesce(label_tt, ''), '')",
    ),
)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def upgrade():
    schema = _quote(op.get_context().opts["catalog_schema"])
    # Each concurrent build commits independently. Do not use IF NOT EXISTS:
    # it would silently accept an invalid index left by an interrupted build.
    # The Alembic README documents inspection and cleanup before retrying.
    with op.get_context().autocommit_block():
        for name, table, expression in INDEXES:
            op.execute(
                DDL(
                    f"CREATE INDEX CONCURRENTLY {_quote(name)} ON {schema}.{_quote(table)} "
                    f"USING gin (to_tsvector('simple'::regconfig, {expression}))"
                )
            )


def downgrade():
    schema = _quote(op.get_context().opts["catalog_schema"])
    with op.get_context().autocommit_block():
        for name, _, _ in reversed(INDEXES):
            op.execute(DDL(f"DROP INDEX CONCURRENTLY IF EXISTS {schema}.{_quote(name)}"))
