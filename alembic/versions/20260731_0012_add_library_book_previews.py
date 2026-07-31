"""add Library book preview manifests

Revision ID: 20260731_0012
Revises: 20260731_0011
Create Date: 2026-07-31 14:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260731_0012"
down_revision = "20260731_0011"
branch_labels = None
depends_on = None

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _schema() -> str:
    context = op.get_context()
    configured = str(context.config.get_main_option("manzara_db_schema") or "").strip()
    value = configured or str(os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip()
    value = value or "monocorpus"
    if not _SCHEMA_RE.match(value):
        raise ValueError(f"Invalid schema name: {value!r}")
    return value


def _qident(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _table(name: str) -> str:
    return f"{_qident(_schema())}.{_qident(name)}"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE {_table("library_book_previews")} (
            md5 TEXT PRIMARY KEY,
            recipe_version TEXT NOT NULL,
            source_page_count INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            manifest_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_run_id BIGINT
                REFERENCES {_table("runs")} (run_id) ON DELETE SET NULL,
            error_text TEXT,
            generated_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CONSTRAINT ck_library_book_previews_page_count
                CHECK (source_page_count IS NULL OR source_page_count > 0),
            CONSTRAINT ck_library_book_previews_attempt_count
                CHECK (attempt_count >= 0),
            CONSTRAINT ck_library_book_previews_status
                CHECK (status IN ('pending', 'processing', 'ready', 'partial', 'failed'))
        )
        """
    )
    schema_literal = _schema().replace("'", "''")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regclass('{schema_literal}.document') IS NOT NULL THEN
                ALTER TABLE {_table("library_book_previews")}
                ADD CONSTRAINT fk_library_book_previews_document
                FOREIGN KEY (md5) REFERENCES {_table("document")} (md5) ON DELETE CASCADE;
            END IF;
        END
        $$
        """
    )
    op.execute(
        f"""
        CREATE INDEX idx_library_book_previews_status
        ON {_table("library_book_previews")} (recipe_version, status, updated_at, md5)
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {_table('library_book_previews')}")
