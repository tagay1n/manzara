"""Store library inclusion on files and derive publication inclusion.

Revision ID: 20261012_0067
Revises: 20261011_0066
"""

from pathlib import Path

from alembic import op
from sqlalchemy import DDL, text

from app.task_runtime.logging import log_message

revision = "20261012_0067"
down_revision = "20261011_0066"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts["catalog_schema"]
    documents = f'"{schema}".catalog_documents'
    publications = f'"{schema}".catalog_publications'
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError("File inclusion requires a resumable online backfill; inspect the revision and SQL file")
    if op.get_bind().execute(text(f'''SELECT EXISTS(SELECT 1 FROM "{schema}".catalog_proposals
            WHERE status IN ('pending','deferred')
              AND field_changes ?| ARRAY['inclusion','lib','evaluation_method'])''')).scalar_one():
        raise RuntimeError('Resolve pending inclusion/evaluation proposals before moving inclusion to files')

    # Commit staging and each bounded backfill independently. The old adapters
    # remain authoritative until the final transactional cutover below.
    with context.autocommit_block():
        connection = op.get_bind()
        old_timeout = connection.execute(text('SHOW lock_timeout')).scalar_one()
        connection.execute(text("SET lock_timeout='10s'"))
        try:
            connection.execute(text(f'''ALTER TABLE {documents}
                ADD COLUMN IF NOT EXISTS inclusion text,
                ADD COLUMN IF NOT EXISTS evaluation_method text'''))
            after_md5 = ''
            migrated = 0
            while True:
                batch = connection.execute(text(f'''
                    WITH candidates AS MATERIALIZED (
                        SELECT d.md5,
                            CASE WHEN d.restricted THEN 'excluded' ELSE p.inclusion END AS inclusion,
                            p.evaluation_method
                        FROM {documents} d JOIN {publications} p USING(publication_id)
                        WHERE d.md5>:after_md5 AND (
                            d.inclusion IS DISTINCT FROM CASE WHEN d.restricted THEN 'excluded' ELSE p.inclusion END
                            OR d.evaluation_method IS DISTINCT FROM p.evaluation_method)
                        ORDER BY d.md5 LIMIT 500 FOR UPDATE OF d
                    ), changed AS (
                        UPDATE {documents} d SET inclusion=c.inclusion,evaluation_method=c.evaluation_method,
                            revision=d.revision+1,updated_at=CURRENT_TIMESTAMP
                        FROM candidates c WHERE c.md5=d.md5 RETURNING d.md5
                    ) SELECT count(*) AS count,max(md5) AS last_md5 FROM changed
                '''), {'after_md5': after_md5}).mappings().one()
                if not batch['count']:
                    break
                migrated += batch['count']
                after_md5 = batch['last_md5']
                if migrated % 5000 == 0:
                    log_message(f'File inclusion backfill: {migrated} files committed')
            log_message(f'File inclusion backfill complete: {migrated} files committed; applying schema cutover')
        finally:
            if not connection.invalidated:
                connection.execute(text("SELECT set_config('lock_timeout',:timeout,false)"), {'timeout': old_timeout})

    path = Path(__file__).resolve().parents[1] / "sql" / "file_library_inclusion_0067.sql"
    sql = path.read_text(encoding="utf-8")
    op.execute(DDL(sql.replace("__CATALOG_SCHEMA__", schema).replace("%", "%%")))


def downgrade():
    raise RuntimeError("Collapsing independent file inclusion requires explicit owner review")
