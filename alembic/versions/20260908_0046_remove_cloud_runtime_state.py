"""remove disposable runtime state from the cloud database

Revision ID: 20260908_0046
Revises: 20260902_0045
"""

from __future__ import annotations

import os
import re

from alembic import op

revision = "20260908_0046"
down_revision = "20260902_0045"
branch_labels = None
depends_on = None
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _schema() -> str:
    value = str(
        op.get_context().config.get_main_option("manzara_db_schema")
        or os.environ.get("MANZARA_DB_SCHEMA")
        or "monocorpus"
    ).strip()
    if not _SCHEMA_RE.fullmatch(value):
        raise ValueError(f"Invalid schema name: {value!r}")
    return value


def _table(name: str) -> str:
    return f'"{_schema()}"."{name}"'


def upgrade() -> None:
    runs = _table("runs")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM {runs}
                WHERE status IN (
                    'starting', 'running', 'stopping_graceful', 'stopping_force'
                )
            ) THEN
                RAISE EXCEPTION
                    'Cannot remove cloud runtime state while task runs are active';
            END IF;
        END
        $$
        """
    )

    for table, constraint in (
        ("library_book_previews", "library_book_previews_last_run_id_fkey"),
        (
            "library_non_pdf_extraction_state",
            "library_non_pdf_extraction_state_last_run_id_fkey",
        ),
        (
            "maintenance_content_migration",
            "maintenance_content_migration_last_run_id_fkey",
        ),
        (
            "maintenance_content_migration_images",
            "maintenance_content_migration_images_last_run_id_fkey",
        ),
    ):
        op.execute(
            f"ALTER TABLE IF EXISTS {_table(table)} "
            f'DROP CONSTRAINT IF EXISTS "{constraint}"'
        )

    for table in (
        "library_book_previews",
        "library_non_pdf_extraction_state",
        "maintenance_content_migration",
        "maintenance_content_migration_images",
        "library_metadata_quality_state",
    ):
        op.execute(f"UPDATE {_table(table)} SET last_run_id = NULL")
    op.execute(f"UPDATE {_table('document_cleanup_queue')} SET run_id = NULL")
    op.execute(
        f"UPDATE {_table('library_collection_validation_attempts')} SET run_id = NULL"
    )

    for table in (
        "library_metadata_extraction_state",
        "library_metadata_evaluation_state",
        "conveyor_run_items",
        "conveyor_runs",
        "conveyor_definitions",
        "gemini_key_model_state",
        "gemini_account_leases",
        "gemini_model_runtime",
        "gemini_runtime_control",
        "gemini_keys",
        "run_logs",
        "events",
        "runs",
        "task_definitions",
        "panel_definitions",
    ):
        op.execute(f"DROP TABLE IF EXISTS {_table(table)} CASCADE")


def downgrade() -> None:
    raise RuntimeError(
        "20260908_0046 is irreversible: disposable cloud runtime data was deleted"
    )
