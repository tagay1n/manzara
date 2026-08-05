"""add durable source aliases for merged library collections

Revision ID: 20260805_0015
Revises: 20260804_0014
Create Date: 2026-08-05 19:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260805_0015"
down_revision = "20260804_0014"
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
    value = value or "monocorpus"
    if not _SCHEMA_RE.fullmatch(value):
        raise ValueError(f"Invalid schema name: {value!r}")
    return value


def _qident(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _table(name: str) -> str:
    return f"{_qident(_schema())}.{_qident(name)}"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE {_table("library_collection_source_aliases")} (
            source_key TEXT PRIMARY KEY,
            collection_id BIGINT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CONSTRAINT fk_library_collection_source_alias_collection
                FOREIGN KEY (collection_id)
                REFERENCES {_table("library_collections")}(collection_id)
                ON DELETE CASCADE
        )
        """
    )
    op.execute(
        f"""
        CREATE INDEX {_qident("idx_library_collection_source_alias_owner")}
        ON {_table("library_collection_source_aliases")} (collection_id)
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {_table('library_collection_source_aliases')}")
