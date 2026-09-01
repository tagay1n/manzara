"""persist content-aware Library preview page roles

Revision ID: 20260901_0044
Revises: 20260830_0043
Create Date: 2026-09-01 08:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260901_0044"
down_revision = "20260830_0043"
branch_labels = None
depends_on = None

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


def _table() -> str:
    return f'"{_schema()}"."library_book_previews"'


def upgrade() -> None:
    table = _table()
    op.execute(f"ALTER TABLE {table} ADD COLUMN first_preview_page INTEGER")
    op.execute(f"ALTER TABLE {table} ADD COLUMN second_preview_page INTEGER")
    op.execute(f"ALTER TABLE {table} ADD COLUMN last_preview_page INTEGER")
    for role in ("first", "second", "last"):
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT "
            f"ck_library_book_previews_{role}_page "
            f"CHECK ({role}_preview_page IS NULL OR "
            f"({role}_preview_page > 0 AND source_page_count IS NOT NULL "
            f"AND {role}_preview_page <= source_page_count))"
        )
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT "
        "ck_library_book_previews_distinct_selected_pages CHECK ("
        "(first_preview_page IS NULL OR second_preview_page IS NULL "
        "OR first_preview_page <> second_preview_page) AND "
        "(first_preview_page IS NULL OR last_preview_page IS NULL "
        "OR first_preview_page <> last_preview_page) AND "
        "(second_preview_page IS NULL OR last_preview_page IS NULL "
        "OR second_preview_page <> last_preview_page))"
    )


def downgrade() -> None:
    table = _table()
    op.execute(
        f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "
        "ck_library_book_previews_distinct_selected_pages"
    )
    for role in ("last", "second", "first"):
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "
            f"ck_library_book_previews_{role}_page"
        )
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {role}_preview_page")
