"""remove retired upstream metadata import provenance

Revision ID: 20260911_0047
Revises: 20260908_0046
Create Date: 2026-09-11 00:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260911_0047"
down_revision = "20260908_0046"
branch_labels = None
depends_on = None

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RETIRED_COLUMNS = (
    "source_key",
    "source_etag",
    "source_size",
    "source_last_modified",
    "payload_sha256",
    "imported_at",
    "updated_at",
)


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
    table = _table("library_upstream_metadata")
    for column in _RETIRED_COLUMNS:
        op.execute(f'ALTER TABLE {table} DROP COLUMN IF EXISTS "{column}"')


def downgrade() -> None:
    table = _table("library_upstream_metadata")
    op.execute(f'ALTER TABLE {table} ADD COLUMN source_key TEXT UNIQUE')
    op.execute(f'ALTER TABLE {table} ADD COLUMN source_etag TEXT')
    op.execute(f'ALTER TABLE {table} ADD COLUMN source_size BIGINT')
    op.execute(f'ALTER TABLE {table} ADD COLUMN source_last_modified TIMESTAMPTZ')
    op.execute(f'ALTER TABLE {table} ADD COLUMN payload_sha256 TEXT')
    op.execute(f'ALTER TABLE {table} ADD COLUMN imported_at TIMESTAMPTZ')
    op.execute(f'ALTER TABLE {table} ADD COLUMN updated_at TIMESTAMPTZ')

