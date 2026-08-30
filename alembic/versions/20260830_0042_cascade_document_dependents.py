"""enforce cascading ownership for document-dependent rows

Revision ID: 20260830_0042
Revises: 20260830_0041
Create Date: 2026-08-30 12:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "20260830_0042"
down_revision = "20260830_0041"
branch_labels = None
depends_on = None

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONFIGURED_SCHEMA_DEPENDENTS = (
    "library_book_previews",
    "library_collection_document_features",
    "library_collection_items",
    "library_collection_proposal_items",
    "library_metadata_evaluation_state",
    "library_metadata_extraction_state",
    "library_metadata_quality_state",
    "library_non_pdf_extraction_state",
    "library_upstream_metadata",
)
_DOCUMENT_SCHEMA_DEPENDENTS = (
    "isbn_keep_many",
    "metadata",
)


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


def _schema_literal() -> str:
    return _schema().replace("'", "''")


def _ensure_document_key(configured: str) -> None:
    op.execute(
        f"""
        DO $$
        DECLARE
            target_schema TEXT;
            document_oid REGCLASS;
            md5_attnum SMALLINT;
        BEGIN
            IF to_regclass('"{configured}".document') IS NOT NULL THEN
                target_schema := '{configured}';
            ELSIF '{configured}' = 'monocorpus'
              AND to_regclass('public.document') IS NOT NULL THEN
                target_schema := 'public';
            ELSE
                RETURN;
            END IF;

            document_oid := to_regclass(format('%I.document', target_schema));
            SELECT attnum INTO md5_attnum
            FROM pg_attribute
            WHERE attrelid = document_oid
              AND attname = 'md5'
              AND NOT attisdropped;
            IF md5_attnum IS NULL THEN
                RAISE EXCEPTION '%.document has no md5 column', target_schema;
            END IF;

            EXECUTE format(
                'ALTER TABLE %I.document ALTER COLUMN md5 SET NOT NULL',
                target_schema
            );
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conrelid = document_oid
                  AND contype IN ('p', 'u')
                  AND conkey = ARRAY[md5_attnum]::SMALLINT[]
            ) THEN
                EXECUTE format(
                    'ALTER TABLE %I.document '
                    'ADD CONSTRAINT uq_document_md5 UNIQUE (md5)',
                    target_schema
                );
            END IF;
        END
        $$
        """
    )


def _add_cascade(
    configured: str,
    table_name: str,
    *, alongside_document: bool,
) -> None:
    constraint_name = f"fk_{table_name}_document_md5"
    child_schema = "target_schema" if alongside_document else f"'{configured}'"
    op.execute(
        f"""
        DO $$
        DECLARE
            target_schema TEXT;
            dependent_schema TEXT;
            dependent_oid REGCLASS;
        BEGIN
            IF to_regclass('"{configured}".document') IS NOT NULL THEN
                target_schema := '{configured}';
            ELSIF '{configured}' = 'monocorpus'
              AND to_regclass('public.document') IS NOT NULL THEN
                target_schema := 'public';
            ELSE
                RETURN;
            END IF;

            dependent_schema := {child_schema};
            dependent_oid := to_regclass(
                format('%I.%I', dependent_schema, '{table_name}')
            );
            IF dependent_oid IS NULL THEN
                RETURN;
            END IF;

            EXECUTE format(
                'DELETE FROM %I.%I AS dependent '
                'WHERE NOT EXISTS ('
                'SELECT 1 FROM %I.document AS document '
                'WHERE document.md5 = dependent.md5)',
                dependent_schema,
                '{table_name}',
                target_schema
            );
            EXECUTE format(
                'ALTER TABLE %I.%I DROP CONSTRAINT IF EXISTS %I',
                dependent_schema,
                '{table_name}',
                '{constraint_name}'
            );
            EXECUTE format(
                'ALTER TABLE %I.%I ADD CONSTRAINT %I '
                'FOREIGN KEY (md5) REFERENCES %I.document(md5) ON DELETE CASCADE',
                dependent_schema,
                '{table_name}',
                '{constraint_name}',
                target_schema
            );
        END
        $$
        """
    )


def _drop_cascade(
    configured: str,
    table_name: str,
    *, alongside_document: bool,
) -> None:
    constraint_name = f"fk_{table_name}_document_md5"
    child_schema = "target_schema" if alongside_document else f"'{configured}'"
    op.execute(
        f"""
        DO $$
        DECLARE
            target_schema TEXT;
            dependent_schema TEXT;
        BEGIN
            IF to_regclass('"{configured}".document') IS NOT NULL THEN
                target_schema := '{configured}';
            ELSIF '{configured}' = 'monocorpus'
              AND to_regclass('public.document') IS NOT NULL THEN
                target_schema := 'public';
            ELSE
                RETURN;
            END IF;
            dependent_schema := {child_schema};
            IF to_regclass(format('%I.%I', dependent_schema, '{table_name}')) IS NULL THEN
                RETURN;
            END IF;
            EXECUTE format(
                'ALTER TABLE %I.%I DROP CONSTRAINT IF EXISTS %I',
                dependent_schema,
                '{table_name}',
                '{constraint_name}'
            );
        END
        $$
        """
    )


def upgrade() -> None:
    configured = _schema_literal()
    _ensure_document_key(configured)
    for table_name in _CONFIGURED_SCHEMA_DEPENDENTS:
        _add_cascade(configured, table_name, alongside_document=False)
    for table_name in _DOCUMENT_SCHEMA_DEPENDENTS:
        _add_cascade(configured, table_name, alongside_document=True)


def downgrade() -> None:
    configured = _schema_literal()
    for table_name in reversed(_DOCUMENT_SCHEMA_DEPENDENTS):
        _drop_cascade(configured, table_name, alongside_document=True)
    for table_name in reversed(_CONFIGURED_SCHEMA_DEPENDENTS):
        _drop_cascade(configured, table_name, alongside_document=False)
