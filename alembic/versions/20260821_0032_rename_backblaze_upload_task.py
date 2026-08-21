"""rename Backblaze transfer task for its upload-only responsibility

Revision ID: 20260821_0032
Revises: 20260820_0031
Create Date: 2026-08-21 12:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260821_0032"
down_revision = "20260820_0031"
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


def _rename(title: str) -> None:
    escaped = title.replace("'", "''")
    op.execute(
        f'UPDATE "{_schema()}"."task_definitions" '
        f"SET title='{escaped}', updated_at=CURRENT_TIMESTAMP::text "
        "WHERE task_id='maintenance.sync_documents_s3'"
    )


def upgrade() -> None:
    _rename("Upload to Backblaze S3")


def downgrade() -> None:
    _rename("Migrate to Backblaze S3")
