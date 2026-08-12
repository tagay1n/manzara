"""add direct Shayan WebDAV upload checkpoints

Revision ID: 20260812_0023
Revises: 20260812_0022
Create Date: 2026-08-12 19:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260812_0023"
down_revision = "20260812_0022"
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


def upgrade() -> None:
    table = _table("shayan_manifest_entries")
    for statement in (
        "ADD COLUMN IF NOT EXISTS webdav_status TEXT NOT NULL DEFAULT 'pending'",
        "ADD COLUMN IF NOT EXISTS webdav_remote_path TEXT",
        "ADD COLUMN IF NOT EXISTS webdav_source_md5 TEXT",
        "ADD COLUMN IF NOT EXISTS webdav_source_size BIGINT",
        "ADD COLUMN IF NOT EXISTS webdav_target_etag TEXT",
        "ADD COLUMN IF NOT EXISTS webdav_target_checksum TEXT",
        "ADD COLUMN IF NOT EXISTS webdav_uploaded_payload_hash TEXT",
        "ADD COLUMN IF NOT EXISTS webdav_uploaded_at TEXT",
        "ADD COLUMN IF NOT EXISTS webdav_last_attempt_at TEXT",
        "ADD COLUMN IF NOT EXISTS webdav_last_error TEXT",
    ):
        op.execute(f"ALTER TABLE {table} {statement}")
    op.execute(
        f"""
        UPDATE {table}
        SET webdav_status = 'legacy_yadisk'
        WHERE yadisk_status = 'uploaded'
          AND COALESCE(yadisk_remote_path, '') <> ''
          AND COALESCE(yadisk_uploaded_payload_hash, '') = COALESCE(payload_hash, '')
          AND webdav_status = 'pending'
        """
    )
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS "idx_shayan_manifest_entries_webdav_status"
        ON {table} (webdav_status, updated_at DESC)
        """
    )
    op.execute(
        f"""
        UPDATE {_table('task_definitions')}
        SET title = 'Upload', updated_at = CURRENT_TIMESTAMP::text
        WHERE task_id = 'shayan.upload_yadisk'
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE {_table('task_definitions')}
        SET title = 'Upload to Yandex Disk', updated_at = CURRENT_TIMESTAMP::text
        WHERE task_id = 'shayan.upload_yadisk'
        """
    )
    table = _table("shayan_manifest_entries")
    op.execute('DROP INDEX IF EXISTS "' + _schema() + '"."idx_shayan_manifest_entries_webdav_status"')
    for column in (
        "webdav_last_error",
        "webdav_last_attempt_at",
        "webdav_uploaded_at",
        "webdav_uploaded_payload_hash",
        "webdav_target_checksum",
        "webdav_target_etag",
        "webdav_source_size",
        "webdav_source_md5",
        "webdav_remote_path",
        "webdav_status",
    ):
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
