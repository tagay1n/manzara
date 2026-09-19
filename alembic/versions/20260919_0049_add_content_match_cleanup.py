"""add resumable matched content cleanup state

Revision ID: 20260919_0049
Revises: 20260911_0048
Create Date: 2026-09-19 12:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260919_0049"
down_revision = "20260911_0048"
branch_labels = None
depends_on = None
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _schema() -> str:
    value = str(
        op.get_context().config.get_main_option("manzara_db_schema")
        or os.environ.get("MANZARA_DB_SCHEMA")
        or "monocorpus"
    ).strip()
    if not _SCHEMA_RE.fullmatch(value):
        raise ValueError(f"Invalid schema name: {value!r}")
    return value


def _table(name: str) -> str:
    return f'"{_schema()}"."{name}"'


def upgrade() -> None:
    table = _table("maintenance_content_match_cleanup")
    op.execute(
        f"""
        CREATE TABLE {table} (
            source_key TEXT PRIMARY KEY,
            md5 TEXT NOT NULL,
            source_etag TEXT NOT NULL,
            source_size BIGINT NOT NULL,
            image_keys_json JSONB NOT NULL DEFAULT '[]'::JSONB,
            deleted_images_json JSONB NOT NULL DEFAULT '[]'::JSONB,
            source_archive_deleted BOOLEAN NOT NULL DEFAULT FALSE,
            status TEXT NOT NULL DEFAULT 'matched',
            error_text TEXT,
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_content_match_cleanup_status CHECK (
                status IN ('matched', 'deleting', 'failed', 'completed')
            ),
            CONSTRAINT ck_content_match_cleanup_size CHECK (source_size >= 0),
            CONSTRAINT ck_content_match_cleanup_image_keys CHECK (
                jsonb_typeof(image_keys_json) = 'array'
                AND jsonb_typeof(deleted_images_json) = 'array'
            )
        )
        """
    )
    op.execute(
        f"CREATE INDEX idx_content_match_cleanup_status "
        f"ON {table} (status, updated_at, source_key)"
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {_table('maintenance_content_match_cleanup')}")
