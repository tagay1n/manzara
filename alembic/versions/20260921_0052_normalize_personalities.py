"""store structured canonical personality normalizations

Revision ID: 20260921_0052
Revises: 20260920_0051
Create Date: 2026-09-21 12:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op

revision = "20260921_0052"
down_revision = "20260920_0051"
branch_labels = None
depends_on = None
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _table(name: str) -> str:
    schema = str(op.get_context().config.get_main_option("manzara_db_schema") or os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip()
    if not _SCHEMA_RE.fullmatch(schema):
        raise ValueError(f"Invalid schema name: {schema!r}")
    return f'"{schema}"."{name}"'


def upgrade() -> None:
    canonicals = _table("normalization_canonicals")
    aliases = _table("normalization_aliases")
    for column in (
        "surname_full TEXT",
        "surname_initials TEXT",
        "name_full TEXT",
        "name_initials TEXT",
        "father_name_full TEXT",
        "father_name_initials TEXT",
        "title TEXT",
        "sex TEXT",
        "identity_key TEXT",
    ):
        op.execute(f"ALTER TABLE {canonicals} ADD COLUMN IF NOT EXISTS {column}")
    for column in (
        "surname_full TEXT",
        "surname_initials TEXT",
        "name_full TEXT",
        "name_initials TEXT",
        "father_name_full TEXT",
        "father_name_initials TEXT",
        "title TEXT",
        "sex TEXT",
        "source_roles JSONB NOT NULL DEFAULT '[]'::jsonb",
        "successful_model TEXT",
        "prompt_version TEXT",
        "schema_version TEXT",
    ):
        op.execute(f"ALTER TABLE {aliases} ADD COLUMN IF NOT EXISTS {column}")
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_table('personality_normalization_checkpoints')} (
            raw_name TEXT PRIMARY KEY,
            source_fingerprint TEXT NOT NULL,
            document_count BIGINT NOT NULL DEFAULT 0,
            mention_count BIGINT NOT NULL DEFAULT 0,
            source_roles JSONB NOT NULL DEFAULT '[]'::jsonb,
            prompt_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            state TEXT NOT NULL,
            attempted_models JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            failure_context TEXT,
            retryable BOOLEAN NOT NULL DEFAULT FALSE,
            canonical_id BIGINT REFERENCES {canonicals}(canonical_id),
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS idx_norm_personality_identity "
        f"ON {canonicals}(entity_type, status, identity_key)"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS idx_personality_checkpoint_state "
        f"ON {_table('personality_normalization_checkpoints')}(state, prompt_version, schema_version)"
    )


def downgrade() -> None:
    raise RuntimeError("Personality normalization checkpoints are durable workflow data.")
