"""drop oscar flow tables

Revision ID: 20260327_0010
Revises: 20260326_0009
Create Date: 2026-03-27 10:30:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260327_0010"
down_revision = "20260326_0009"
branch_labels = None
depends_on = None

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _schema() -> str:
    try:
        context = op.get_context()
        cfg = context.config if context is not None else None
        cfg_schema = str(cfg.get_main_option("manzara_db_schema") or "").strip() if cfg else ""
        if cfg_schema:
            value = cfg_schema
        else:
            value = str(os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip()
    except Exception:
        value = str(os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip()
    if not value:
        value = "monocorpus"
    if not _SCHEMA_RE.match(value):
        raise ValueError(f"Invalid schema name: {value!r}")
    return value


def _qident(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _table(name: str) -> str:
    return f"{_qident(_schema())}.{_qident(name)}"


def upgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {_table('oscar_snapshot_stages')}")
    op.execute(f"DROP TABLE IF EXISTS {_table('oscar_snapshots')}")


def downgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_table("oscar_snapshots")} (
            snapshot_id TEXT PRIMARY KEY,
            source_path TEXT,
            source_label TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            status TEXT NOT NULL DEFAULT 'pending',
            discovered_at TEXT,
            claimed_at TEXT,
            completed_at TEXT,
            error_text TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {_qident("idx_oscar_snapshots_status_order")}
        ON {_table("oscar_snapshots")} (status, discovered_at, updated_at)
        """
    )
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_table("oscar_snapshot_stages")} (
            snapshot_id TEXT NOT NULL,
            stage_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            run_id BIGINT,
            started_at TEXT,
            finished_at TEXT,
            error_text TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, stage_name),
            FOREIGN KEY (snapshot_id) REFERENCES {_table("oscar_snapshots")}(snapshot_id) ON DELETE CASCADE,
            FOREIGN KEY (run_id) REFERENCES {_table("runs")}(run_id)
        )
        """
    )
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {_qident("idx_oscar_snapshot_stages_status")}
        ON {_table("oscar_snapshot_stages")} (stage_name, status)
        """
    )
