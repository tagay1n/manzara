"""remove completed Shayan Yandex-to-Hetzner migration task

Revision ID: 20260815_0026
Revises: 20260815_0025
Create Date: 2026-08-15 16:00:00
"""

from __future__ import annotations

import json
import os
import re

from alembic import op
from sqlalchemy import text


revision = "20260815_0026"
down_revision = "20260815_0025"
branch_labels = None
depends_on = None

_TASK_ID = "shayan.transfer_yadisk_webdav"
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _schema() -> str:
    configured = str(
        op.get_context().config.get_main_option("manzara_db_schema") or ""
    ).strip()
    value = (
        configured or str(os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip()
    )
    if not _SCHEMA_RE.fullmatch(value):
        raise ValueError(f"Invalid schema name: {value!r}")
    return value


def _table(name: str) -> str:
    return f'"{_schema()}"."{name}"'


def _remove_from_conveyor_definition() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        text(f"SELECT conveyor_id, stages_json FROM {_table('conveyor_definitions')}")
    ).mappings()
    for row in rows:
        stages = json.loads(str(row["stages_json"] or "[]"))
        filtered_stages = []
        changed = False
        for stage in stages if isinstance(stages, list) else []:
            if not isinstance(stage, dict):
                continue
            items = stage.get("items")
            if not isinstance(items, list):
                items = []
            filtered_items = [
                item
                for item in items
                if not isinstance(item, dict) or item.get("task_id") != _TASK_ID
            ]
            changed = changed or len(filtered_items) != len(items)
            if filtered_items:
                filtered_stages.append({**stage, "items": filtered_items})
            else:
                changed = changed or bool(items)
        if changed:
            bind.execute(
                text(
                    f"""
                    UPDATE {_table("conveyor_definitions")}
                    SET revision = revision + 1,
                        stages_json = :stages_json,
                        updated_at = CURRENT_TIMESTAMP::text
                    WHERE conveyor_id = :conveyor_id
                    """
                ),
                {
                    "conveyor_id": row["conveyor_id"],
                    "stages_json": json.dumps(filtered_stages, ensure_ascii=False),
                },
            )


def upgrade() -> None:
    _remove_from_conveyor_definition()
    op.execute(
        f"""
        DELETE FROM {_table("conveyor_runs")}
        WHERE conveyor_run_id IN (
            SELECT conveyor_run_id FROM {_table("conveyor_run_items")}
            WHERE task_id = '{_TASK_ID}'
        )
        """
    )
    op.execute(
        f"""
        DELETE FROM {_table("workflow_step_runs")}
        WHERE task_run_id IN (
            SELECT run_id FROM {_table("runs")} WHERE task_id = '{_TASK_ID}'
        )
        """
    )
    op.execute(
        f"DELETE FROM {_table('events')} "
        f"WHERE task_id = '{_TASK_ID}' OR run_id IN ("
        f"SELECT run_id FROM {_table('runs')} WHERE task_id = '{_TASK_ID}')"
    )
    op.execute(f"DELETE FROM {_table('runs')} WHERE task_id = '{_TASK_ID}'")
    op.execute(f"DELETE FROM {_table('task_definitions')} WHERE task_id = '{_TASK_ID}'")
    op.execute(f"DROP TABLE IF EXISTS {_table('shayan_webdav_transfers')}")


def downgrade() -> None:
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
        CREATE INDEX idx_shayan_webdav_transfers_status
        ON {_table("shayan_webdav_transfers")} (status, updated_at, source_path)
        """
    )
