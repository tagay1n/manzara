"""store upstream document metadata in PostgreSQL

Revision ID: 20260830_0041
Revises: 20260828_0040
Create Date: 2026-08-30 11:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260830_0041"
down_revision = "20260828_0040"
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


def _schema_literal() -> str:
    return _schema().replace("'", "''")


def upgrade() -> None:
    table = _table("library_upstream_metadata")
    op.execute(
        f"""
        CREATE TABLE {table} (
            md5 TEXT PRIMARY KEY,
            payload_json JSONB NOT NULL
                CHECK (jsonb_typeof(payload_json) = 'object'),
            source_key TEXT NOT NULL UNIQUE,
            source_etag TEXT NOT NULL,
            source_size BIGINT NOT NULL CHECK (source_size >= 0),
            source_last_modified TIMESTAMPTZ,
            payload_sha256 TEXT NOT NULL,
            imported_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (md5 ~ '^[0-9a-f]{{32}}$'),
            CHECK (payload_sha256 ~ '^[0-9a-f]{{64}}$')
        )
        """
    )
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
                'ALTER TABLE %I.document DROP COLUMN IF EXISTS upstream_meta_url',
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
                'ALTER TABLE %I.document ADD COLUMN IF NOT EXISTS upstream_meta_url TEXT',
                target_schema
            );
        END
        $$
        """
    )
    op.execute(f"DROP TABLE IF EXISTS {_table('library_upstream_metadata')}")
