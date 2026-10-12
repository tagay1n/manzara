"""Normalize publication dates to year-only metadata.

Revision ID: 20261011_0066
Revises: 20261011_0065
"""

from alembic import op
from sqlalchemy import text

revision = "20261011_0066"
down_revision = "20261011_0065"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts["catalog_schema"]
    table = f'"{schema}".catalog_publications'
    connection = op.get_bind()

    unsupported = connection.execute(text(f"""
        SELECT EXISTS (
            SELECT 1 FROM {table}
            WHERE date_published IS NOT NULL
              AND date_published !~ '^[0-9]{{4}}(-[0-9]{{2}}(-[0-9]{{2}})?)?$'
        )
    """)).scalar_one()
    if unsupported:
        raise RuntimeError(
            "Publication-year migration found unsupported date formats; "
            "review date_published before retrying"
        )

    # Each UPDATE autocommits independently. If startup loses its connection,
    # rerunning this revision resumes from the remaining month/day values.
    context = op.get_context()
    with context.autocommit_block():
        while True:
            updated = op.get_bind().execute(text(f"""
                WITH candidates AS MATERIALIZED (
                    SELECT publication_id
                    FROM {table}
                    WHERE date_published ~ '^[0-9]{{4}}-[0-9]{{2}}(-[0-9]{{2}})?$'
                    ORDER BY publication_id
                    LIMIT 500
                    FOR UPDATE
                ), changed AS (
                    UPDATE {table} AS publication
                    SET date_published = left(publication.date_published, 4),
                        revision = publication.revision + 1,
                        updated_at = CURRENT_TIMESTAMP
                    FROM candidates
                    WHERE publication.publication_id = candidates.publication_id
                    RETURNING publication.publication_id
                )
                SELECT count(*) FROM changed
            """)).scalar_one()
            if updated == 0:
                break

    # Stop concurrent legacy writers from reintroducing a month/day value
    # between the last batch and constraint validation.
    op.execute(text(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE"))
    remaining = op.get_bind().execute(text(f"""
        SELECT EXISTS (
            SELECT 1 FROM {table}
            WHERE date_published IS NOT NULL
              AND date_published !~ '^[0-9]{{4}}$'
        )
    """)).scalar_one()
    if remaining:
        raise RuntimeError(
            "Publication-year values appeared during the backfill; "
            "pause catalog writers and retry"
        )

    op.execute(text(f"""
        ALTER TABLE {table}
        ADD CONSTRAINT ck_catalog_publication_year
        CHECK (date_published IS NULL OR date_published ~ '^[0-9]{{4}}$')
    """))


def downgrade():
    raise RuntimeError("Restoring publication month/day precision requires explicit owner review")
