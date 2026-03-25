"""add gemini runtime tables

Revision ID: 20260325_0003
Revises: 20260324_0002
Create Date: 2026-03-25 10:40:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260325_0003"
down_revision = "20260324_0002"
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
    schema = _schema()
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {_qident(schema)}")

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_table("gemini_keys")} (
            key_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            masked_key TEXT NOT NULL,
            active BIGINT NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {_qident("idx_gemini_keys_account")}
        ON {_table("gemini_keys")} (account_id)
        """
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_table("gemini_key_model_state")} (
            key_id TEXT NOT NULL,
            model_name TEXT NOT NULL,
            exhausted BIGINT NOT NULL DEFAULT 0,
            exhausted_at TEXT,
            cooldown_until TEXT,
            last_used_at TEXT,
            last_success_at TEXT,
            last_error_at TEXT,
            last_error_text TEXT,
            attempts_total BIGINT NOT NULL DEFAULT 0,
            attempts_cycle BIGINT NOT NULL DEFAULT 0,
            success_total BIGINT NOT NULL DEFAULT 0,
            success_cycle BIGINT NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (key_id, model_name),
            FOREIGN KEY (key_id) REFERENCES {_table("gemini_keys")}(key_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {_qident("idx_gemini_state_model")}
        ON {_table("gemini_key_model_state")} (model_name)
        """
    )
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {_qident("idx_gemini_state_model_exhausted")}
        ON {_table("gemini_key_model_state")} (model_name, exhausted)
        """
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_table("gemini_runtime_control")} (
            control_id BIGINT PRIMARY KEY,
            cycle_label TEXT NOT NULL,
            pause_until TEXT,
            last_pause_reason TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {_table('gemini_runtime_control')}")
    op.execute(f"DROP TABLE IF EXISTS {_table('gemini_key_model_state')}")
    op.execute(f"DROP TABLE IF EXISTS {_table('gemini_keys')}")

