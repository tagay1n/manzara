"""detach upstream metadata from document cascade

Revision ID: 20260911_0048
Revises: 20260911_0047
Create Date: 2026-09-11 00:30:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260911_0048"
down_revision = "20260911_0047"
branch_labels = None
depends_on = None

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONSTRAINT = "fk_library_upstream_metadata_document_md5"


def _schema() -> str:
    configured = str(
        op.get_context().config.get_main_option("manzara_db_schema") or ""
    ).strip()
    value = configured or str(
        os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus"
    ).strip()
    if not _SCHEMA_RE.fullmatch(value):
        raise ValueError(f"Invalid schema name: {value!r}")
    return value


def _table(name: str) -> str:
    return f'"{_schema()}"."{name}"'


def _schema_literal() -> str:
    return _schema().replace("'", "''")


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {_table('library_upstream_metadata')} "
        f'DROP CONSTRAINT IF EXISTS "{_CONSTRAINT}"'
    )


def downgrade() -> None:
    configured = _schema_literal()
    table = _table("library_upstream_metadata")
    op.execute(
        f"""
        DO $$
        DECLARE target_schema TEXT;
        BEGIN
            IF to_regclass('"{configured}".document') IS NOT NULL THEN
                target_schema := '{configured}';
            ELSIF '{configured}' = 'monocorpus'
              AND to_regclass('public.document') IS NOT NULL THEN
                target_schema := 'public';
            ELSE
                RETURN;
            END IF;
            EXECUTE format(
                'DELETE FROM {table} AS upstream '
                'WHERE NOT EXISTS ('
                'SELECT 1 FROM %I.document AS document '
                'WHERE document.md5 = upstream.md5)',
                target_schema
            );
            EXECUTE format(
                'ALTER TABLE {table} ADD CONSTRAINT {_CONSTRAINT} '
                'FOREIGN KEY (md5) REFERENCES %I.document(md5) ON DELETE CASCADE',
                target_schema
            );
        END
        $$
        """
    )

