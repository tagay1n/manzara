"""Durable publisher group analysis and owner review.

Revision ID: 20261001_0054
Revises: 20260929_0053
"""

from __future__ import annotations

import os
import re
from alembic import op

revision = "20261001_0054"
down_revision = "20260929_0053"
branch_labels = None
depends_on = None


def _table(name):
    schema = (
        op.get_context().config.get_main_option("manzara_db_schema")
        or os.environ.get("MANZARA_DB_SCHEMA")
        or "monocorpus"
    )
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("Invalid schema name")
    return f'"{schema}"."{name}"'


def upgrade():
    op.execute(f"""CREATE TABLE {_table("publisher_merge_analyses")} (
        analysis_id BIGSERIAL PRIMARY KEY, fingerprint TEXT NOT NULL, scope TEXT NOT NULL,
        inventory JSONB NOT NULL, metadata JSONB NOT NULL, response JSONB,
        state TEXT NOT NULL CHECK (state IN ('generating','checkpointed','imported','failed')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    op.execute(f"""CREATE TABLE {_table("publisher_merge_proposals")} (
        proposal_id BIGSERIAL PRIMARY KEY,
        analysis_id BIGINT NOT NULL REFERENCES {_table("publisher_merge_analyses")}(analysis_id),
        fingerprint TEXT NOT NULL, proposal JSONB NOT NULL, members JSONB NOT NULL, review_edit JSONB,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','staged','skipped','separate','applied')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    op.execute(f"""CREATE UNIQUE INDEX publisher_merge_pending_fingerprint ON {_table("publisher_merge_proposals")}(fingerprint)
        WHERE status IN ('pending','staged','skipped')""")
    op.execute(f"""CREATE TABLE {_table("publisher_review_draft")} (
        singleton INTEGER PRIMARY KEY CHECK (singleton=1), revision BIGINT NOT NULL DEFAULT 0,
        changes JSONB NOT NULL, reviewed JSONB NOT NULL, proposal_ids JSONB NOT NULL,
        updated_at TEXT NOT NULL)""")
    op.execute(f"""CREATE TABLE {_table("publisher_separations")} (
        left_key TEXT NOT NULL, right_key TEXT NOT NULL, provenance JSONB NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(left_key,right_key), CHECK(left_key < right_key))""")
    op.execute(f"""INSERT INTO {_table("publisher_review_draft")}
        VALUES (1,0,'{{"renames":[],"keeps":[],"merges":[]}}'::jsonb,'{{}}'::jsonb,'[]'::jsonb,'')""")


def downgrade():
    raise RuntimeError(
        "Publisher identity and review checkpoints are durable workflow data."
    )
