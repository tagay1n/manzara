"""add resumable non-PDF content extraction state

Revision ID: 20260825_0034
Revises: 20260824_0033
Create Date: 2026-08-25 12:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260825_0034"
down_revision = "20260824_0033"
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
        CREATE TABLE {table} (
            md5 TEXT PRIMARY KEY,
            extractor_version TEXT NOT NULL,
            detected_format TEXT,
            status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_run_id BIGINT REFERENCES {_table("runs")} (run_id) ON DELETE SET NULL,
            error_text TEXT,
            generated_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_library_non_pdf_extraction_status
                CHECK (status IN ('processing', 'ready', 'failed', 'unsupported')),
            CONSTRAINT ck_library_non_pdf_extraction_attempts
                CHECK (attempt_count >= 0),
            CONSTRAINT fk_library_non_pdf_extraction_document
                FOREIGN KEY (md5) REFERENCES {_table("document")} (md5) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        f"CREATE INDEX idx_library_non_pdf_extraction_queue "
        f"ON {table} (extractor_version, status, updated_at, md5)"
    )


def downgrade() -> None:
    op.execute(
        f"DROP TABLE IF EXISTS {_table('library_non_pdf_extraction_state')}"
    )
