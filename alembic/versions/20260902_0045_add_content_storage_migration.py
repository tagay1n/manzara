"""add resumable PDF content storage migration state

Revision ID: 20260902_0045
Revises: 20260901_0044
Create Date: 2026-09-02 12:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260902_0045"
down_revision = "20260901_0044"
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
    state = _table("maintenance_content_migration")
    images = _table("maintenance_content_migration_images")
    op.execute(
        f"""
        CREATE TABLE {state} (
            md5 TEXT PRIMARY KEY,
            source_content_url TEXT NOT NULL,
            source_mime_type TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            source_archive_etag TEXT,
            source_archive_size BIGINT,
            destination_content_url TEXT,
            destination_archive_etag TEXT,
            destination_archive_size BIGINT,
            destination_archive_sha256 TEXT,
            markdown_member TEXT,
            source_archive_deleted BOOLEAN NOT NULL DEFAULT FALSE,
            last_run_id BIGINT REFERENCES {_table("runs")} (run_id)
                ON DELETE SET NULL,
            error_text TEXT,
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_maintenance_content_migration_status CHECK (
                status IN ('copying', 'cutover', 'deleting', 'failed', 'completed')
            ),
            CONSTRAINT ck_maintenance_content_migration_attempts
                CHECK (attempt_count >= 0),
            CONSTRAINT ck_maintenance_content_migration_sizes CHECK (
                (source_archive_size IS NULL OR source_archive_size >= 0)
                AND (destination_archive_size IS NULL
                     OR destination_archive_size >= 0)
            )
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE {images} (
            md5 TEXT NOT NULL REFERENCES {state} (md5) ON DELETE CASCADE,
            image_key TEXT NOT NULL,
            source_etag TEXT NOT NULL,
            source_size BIGINT NOT NULL,
            destination_etag TEXT NOT NULL,
            destination_size BIGINT NOT NULL,
            sha256 TEXT NOT NULL,
            source_deleted BOOLEAN NOT NULL DEFAULT FALSE,
            last_run_id BIGINT REFERENCES {_table("runs")} (run_id)
                ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (md5, image_key),
            CONSTRAINT ck_maintenance_content_migration_image_sizes CHECK (
                source_size >= 0 AND destination_size >= 0
            )
        )
        """
    )
    schema_literal = _schema().replace("'", "''")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regclass('{schema_literal}.document') IS NOT NULL THEN
                ALTER TABLE {state}
                ADD CONSTRAINT fk_maintenance_content_migration_document
                FOREIGN KEY (md5) REFERENCES {_table("document")} (md5)
                ON DELETE CASCADE;
            END IF;
        END
        $$
        """
    )
    op.execute(
        f"CREATE INDEX idx_maintenance_content_migration_queue "
        f"ON {state} (status, updated_at, md5)"
    )


def downgrade() -> None:
    op.execute(
        f"DROP TABLE IF EXISTS {_table('maintenance_content_migration_images')}"
    )
    op.execute(f"DROP TABLE IF EXISTS {_table('maintenance_content_migration')}")
