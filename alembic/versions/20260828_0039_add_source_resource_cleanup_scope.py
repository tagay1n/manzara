"""add source-resource cleanup scope

Revision ID: 20260828_0039
Revises: 20260827_0038
Create Date: 2026-08-28 12:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260828_0039"
down_revision = "20260827_0038"
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
    return f'"{_schema()}"."document_cleanup_queue"'


def upgrade() -> None:
    queue = _table()
    op.execute(
        f"ALTER TABLE {queue} DROP CONSTRAINT document_cleanup_queue_scope_check"
    )
    op.execute(
        f"ALTER TABLE {queue} ADD CONSTRAINT document_cleanup_queue_scope_check "
        "CHECK (scope IN ('document', 'duplicate_resource', 'source_resource'))"
    )
    op.execute(
        f"CREATE UNIQUE INDEX uq_document_cleanup_active_source_path ON {queue} "
        "(scope, source_path) WHERE scope = 'source_resource' "
        "AND status IN ('planned', 'running', 'failed')"
    )


def downgrade() -> None:
    queue = _table()
    op.execute(
        f'DROP INDEX IF EXISTS "{_schema()}".uq_document_cleanup_active_source_path'
    )
    op.execute(
        f"UPDATE {queue} SET scope = 'duplicate_resource' "
        "WHERE scope = 'source_resource'"
    )
    op.execute(
        f"ALTER TABLE {queue} DROP CONSTRAINT document_cleanup_queue_scope_check"
    )
    op.execute(
        f"ALTER TABLE {queue} ADD CONSTRAINT document_cleanup_queue_scope_check "
        "CHECK (scope IN ('document', 'duplicate_resource'))"
    )
