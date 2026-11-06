"""Add the publication metadata review queue, accepting completed manual steps."""
import re

from alembic import op
from sqlalchemy import text

revision = "20261106_0069"
down_revision = "20261013_0068"
branch_labels = None
depends_on = None


def _ensure_column(connection, schema, publications):
    column = connection.execute(text("""
        SELECT data_type,is_nullable,column_default FROM information_schema.columns
        WHERE table_schema=:schema AND table_name='catalog_publications'
          AND column_name='metadata_review_order'
    """), {'schema': schema}).mappings().first()
    if column is None:
        connection.execute(text(f"""ALTER TABLE {publications}
            ADD COLUMN metadata_review_order bigint NOT NULL DEFAULT 0"""))
        return
    default = re.sub(r"[\s()']", '', column['column_default'] or '')
    if (column['data_type'] != 'bigint' or column['is_nullable'] != 'NO'
            or default not in {'0', '0::bigint', '0::integer'}):
        raise RuntimeError('Existing metadata_review_order must be bigint NOT NULL DEFAULT 0')


def _ensure_constraint(connection, publications):
    constraint = connection.execute(text("""
        SELECT contype,pg_get_expr(conbin,conrelid) AS expression,convalidated
        FROM pg_constraint WHERE conrelid=CAST(:table AS regclass)
          AND conname='ck_catalog_metadata_review_order'
    """), {'table': publications}).mappings().first()
    if constraint is None:
        connection.execute(text(f"""ALTER TABLE {publications}
            ADD CONSTRAINT ck_catalog_metadata_review_order
            CHECK (metadata_review_order >= -1) NOT VALID"""))
    else:
        expression = re.sub(r"[\s()\"']", '', constraint['expression'] or '')
        expression = expression.replace('::bigint', '').replace('::integer', '')
        if constraint['contype'] != 'c' or expression != 'metadata_review_order>=-1':
            raise RuntimeError('Existing ck_catalog_metadata_review_order has an incompatible definition')
        if constraint['convalidated']:
            return
    connection.execute(text(f"ALTER TABLE {publications} VALIDATE CONSTRAINT ck_catalog_metadata_review_order"))


def _ensure_sequence(connection, schema, publications, sequence):
    exists = connection.execute(text('SELECT to_regclass(:sequence) IS NOT NULL'),
                                {'sequence': sequence}).scalar_one()
    if not exists:
        connection.execute(text(f"CREATE SEQUENCE {sequence} AS bigint START WITH 2 OWNED BY {publications}.metadata_review_order"))
    definition = connection.execute(text("""
        SELECT seqtypid='bigint'::regtype AS bigint_type,seqincrement,seqmin,seqmax,seqcycle,seqcache
        FROM pg_sequence WHERE seqrelid=CAST(:sequence AS regclass)
    """), {'sequence': sequence}).mappings().first()
    if (definition is None or not definition['bigint_type']
            or definition['seqincrement'] != 1 or definition['seqmin'] != 1
            or definition['seqmax'] != 9223372036854775807
            or definition['seqcycle'] or definition['seqcache'] != 1):
        raise RuntimeError('Existing metadata review sequence must be bigint, increment 1, cache 1, and NO CYCLE with default bounds')
    owners = connection.execute(text("""
        SELECT ns.nspname,t.relname,a.attname
        FROM pg_depend d JOIN pg_class t ON t.oid=d.refobjid
        JOIN pg_namespace ns ON ns.oid=t.relnamespace
        JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=d.refobjsubid
        WHERE d.classid='pg_class'::regclass AND d.objid=CAST(:sequence AS regclass)
          AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i')
    """), {'sequence': sequence}).all()
    if owners and list(owners) != [(schema, 'catalog_publications', 'metadata_review_order')]:
        raise RuntimeError('Existing metadata review sequence belongs to another column')
    if not owners:
        connection.execute(text(f'ALTER SEQUENCE {sequence} OWNED BY {publications}.metadata_review_order'))
    minimum_next = connection.execute(text(f"SELECT GREATEST(COALESCE(MAX(metadata_review_order),0)+1,2) FROM {publications}")).scalar_one()
    state = connection.execute(text(f'SELECT last_value,is_called FROM {sequence}')).mappings().one()
    next_value = state['last_value'] + int(state['is_called'])
    if next_value < minimum_next:
        # setval is not transactional. Only move forward, so rollback is harmless.
        connection.execute(text('SELECT setval(CAST(:sequence AS regclass),:value,false)'),
                           {'sequence': sequence, 'value': minimum_next})


def upgrade():
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError('Metadata queue migration requires online catalog inspection')
    schema = context.opts['catalog_schema']
    connection = op.get_bind()
    quoted = connection.dialect.identifier_preparer.quote_identifier(schema)
    publications = f'{quoted}.catalog_publications'
    sequence = f'{quoted}.catalog_metadata_review_order_seq'
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    connection.execute(text(f'LOCK TABLE {publications} IN SHARE ROW EXCLUSIVE MODE'))
    _ensure_column(connection, schema, publications)
    _ensure_constraint(connection, publications)
    # Resume partial backfills without resetting reviewed or admin-protected rows.
    connection.execute(text(f"""UPDATE {publications} SET metadata_review_order=1
        WHERE metadata_present AND metadata_review_order=0"""))
    _ensure_sequence(connection, schema, publications, sequence)


def downgrade():
    raise RuntimeError("Metadata queue migration is forward-only")
