"""replace unused Shayan S3 checkpoints with WebDAV checkpoints

Revision ID: 20260804_0014
Revises: 20260731_0013
Create Date: 2026-08-04 12:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260804_0014"
down_revision = "20260731_0013"
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
    op.execute(f"DROP TABLE IF EXISTS {_table('shayan_s3_transfers')}")
    op.execute(
        f"""
        CREATE TABLE {_table("shayan_webdav_transfers")} (
            source_path TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            source_md5 TEXT NOT NULL,
            source_size BIGINT NOT NULL,
            target_path TEXT NOT NULL UNIQUE,
            target_etag TEXT,
            target_checksum TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            error_text TEXT,
            discovered_at TEXT NOT NULL,
            last_attempt_at TEXT,
            moved_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CONSTRAINT ck_shayan_webdav_transfer_status
                CHECK (status IN ('pending', 'transferring', 'uploaded', 'moved', 'failed')),
            CONSTRAINT ck_shayan_webdav_transfer_size CHECK (source_size >= 0)
        )
        """
    )
    op.execute(
        f"""
        CREATE INDEX {_qident("idx_shayan_webdav_transfers_status")}
        ON {_table("shayan_webdav_transfers")} (status, updated_at, source_path)
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {_table('shayan_webdav_transfers')}")
    op.execute(
        f"""
        CREATE TABLE {_table("shayan_s3_transfers")} (
            source_path TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            source_md5 TEXT NOT NULL,
            source_size BIGINT NOT NULL,
            target_bucket TEXT NOT NULL,
            target_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            error_text TEXT,
            discovered_at TEXT NOT NULL,
            last_attempt_at TEXT,
            moved_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CONSTRAINT ck_shayan_s3_transfer_status
                CHECK (status IN ('pending', 'transferring', 'uploaded', 'moved', 'failed')),
            CONSTRAINT uq_shayan_s3_transfer_target UNIQUE (target_bucket, target_key)
        )
        """
    )
    op.execute(
        f"""
        CREATE INDEX {_qident("idx_shayan_s3_transfers_status")}
        ON {_table("shayan_s3_transfers")} (status, updated_at, source_path)
        """
    )
