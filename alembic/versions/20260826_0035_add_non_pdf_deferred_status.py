"""add deferred non-PDF extraction status

Revision ID: 20260826_0035
Revises: 20260825_0034
Create Date: 2026-08-26 15:30:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260826_0035"
down_revision = "20260825_0034"
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


def _table(name: str) -> str:
    return f'"{_schema()}"."{name}"'


def upgrade() -> None:
    table = _table("library_non_pdf_extraction_state")
    op.execute(
        f"""
        ALTER TABLE {table}
        DROP CONSTRAINT ck_library_non_pdf_extraction_status,
        ADD CONSTRAINT ck_library_non_pdf_extraction_status
            CHECK (status IN (
                'processing', 'ready', 'failed', 'unsupported', 'deferred'
            ))
        """
    )
    op.execute(
        f"""
        UPDATE {table}
        SET status = 'deferred', updated_at = CURRENT_TIMESTAMP
        WHERE status = 'failed'
          AND (
              error_text LIKE '%Extracted document contains only images; OCR required%'
              OR error_text LIKE '%Rendered Markdown validation failed:%'
              OR error_text LIKE '%LibreOffice produced 0 DOCX files%'
              OR error_text LIKE '%couldn''t unpack docx container:%'
          )
        """
    )


def downgrade() -> None:
    table = _table("library_non_pdf_extraction_state")
    op.execute(f"UPDATE {table} SET status = 'failed' WHERE status = 'deferred'")
    op.execute(
        f"""
        ALTER TABLE {table}
        DROP CONSTRAINT ck_library_non_pdf_extraction_status,
        ADD CONSTRAINT ck_library_non_pdf_extraction_status
            CHECK (status IN ('processing', 'ready', 'failed', 'unsupported'))
        """
    )
