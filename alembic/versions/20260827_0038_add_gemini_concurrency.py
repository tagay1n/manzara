"""add Gemini account leases, model pauses, and run worker settings

Revision ID: 20260827_0038
Revises: 20260827_0037
Create Date: 2026-08-27 16:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260827_0038"
down_revision = "20260827_0037"
branch_labels = None
depends_on = None

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _schema() -> str:
    value = str(op.get_context().config.get_main_option("manzara_db_schema") or "").strip()
    value = value or str(os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip()
    if not _SCHEMA_RE.fullmatch(value):
        raise ValueError(f"Invalid schema name: {value!r}")
    return value


def _table(name: str) -> str:
    return f'"{_schema()}"."{name}"'


def upgrade() -> None:
    op.execute(f"ALTER TABLE {_table('task_definitions')} ADD COLUMN IF NOT EXISTS gemini_workers_default INTEGER")
    op.execute(f"ALTER TABLE {_table('task_definitions')} ADD COLUMN IF NOT EXISTS gemini_workers_next INTEGER")
    op.execute(f"ALTER TABLE {_table('runs')} ADD COLUMN IF NOT EXISTS gemini_workers INTEGER")
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_table('gemini_account_leases')} (
            account_id TEXT PRIMARY KEY,
            lease_token TEXT,
            task_id TEXT,
            run_id BIGINT,
            worker_id TEXT,
            lease_expires_at TEXT,
            last_acquired_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_table('gemini_model_runtime')} (
            model_name TEXT PRIMARY KEY,
            pause_until TEXT,
            last_pause_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {_table('gemini_model_runtime')}")
    op.execute(f"DROP TABLE IF EXISTS {_table('gemini_account_leases')}")
    op.execute(f"ALTER TABLE {_table('runs')} DROP COLUMN IF EXISTS gemini_workers")
    op.execute(f"ALTER TABLE {_table('task_definitions')} DROP COLUMN IF EXISTS gemini_workers_next")
    op.execute(f"ALTER TABLE {_table('task_definitions')} DROP COLUMN IF EXISTS gemini_workers_default")
