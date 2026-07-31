"""add primary storage verification fields to documents

Revision ID: 20260731_0013
Revises: 20260731_0012
Create Date: 2026-07-31 18:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260731_0013"
down_revision = "20260731_0012"
branch_labels = None
depends_on = None

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _schema() -> str:
    context = op.get_context()
    configured = str(context.config.get_main_option("manzara_db_schema") or "").strip()
    value = configured or str(os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip()
    value = value or "monocorpus"
    if not _SCHEMA_RE.fullmatch(value):
        raise ValueError(f"Invalid schema name: {value!r}")
    return value


def _schema_literal() -> str:
    return _schema().replace("'", "''")


def upgrade() -> None:
    configured = _schema_literal()
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
                'ALTER TABLE %I.document '
                'ADD COLUMN IF NOT EXISTS primary_storage_size BIGINT, '
                'ADD COLUMN IF NOT EXISTS primary_storage_etag TEXT, '
                'ADD COLUMN IF NOT EXISTS primary_storage_verified_at TIMESTAMPTZ',
                target_schema
            );
            EXECUTE format(
                'ALTER TABLE %I.document '
                'DROP CONSTRAINT IF EXISTS ck_document_primary_storage_size',
                target_schema
            );
            EXECUTE format(
                'ALTER TABLE %I.document ADD CONSTRAINT ck_document_primary_storage_size '
                'CHECK (primary_storage_size IS NULL OR primary_storage_size >= 0)',
                target_schema
            );
        END
        $$
        """
    )


def downgrade() -> None:
    configured = _schema_literal()
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
                'ALTER TABLE %I.document '
                'DROP COLUMN IF EXISTS primary_storage_verified_at, '
                'DROP COLUMN IF EXISTS primary_storage_etag, '
                'DROP COLUMN IF EXISTS primary_storage_size',
                target_schema
            );
        END
        $$
        """
    )
