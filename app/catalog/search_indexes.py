"""Explicit offline search-index staging for constrained catalog imports."""

from sqlalchemy import func, select, text

SEARCH_INDEXES = {
    'publications': ('name', 'description', 'edition'),
    'entities': ('display_name',), 'names': ('raw_name',),
    'collections': ('title',), 'classification_nodes': ('label_en', 'label_tt'),
}


def defer_search_indexes(catalog):
    """Remove only rebuildable search indexes from a verified empty staging area."""
    with catalog.engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout='10s'"))
        conn.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'), {'key': f'catalog-import:{catalog.schema}'})
        for table in sorted(catalog.tables.values(), key=lambda item: item.name):
            conn.execute(text(f'LOCK TABLE "{catalog.schema}"."{table.name}" IN SHARE ROW EXCLUSIVE MODE'))
            if table.name == 'catalog_imports':
                occupied = conn.execute(select(table).where(table.c.state == 'active').limit(1)).first()
            else:
                occupied = conn.execute(select(func.count()).select_from(table)).scalar_one()
            if occupied:
                raise ValueError('search indexes may be deferred only for empty staging')
        for table, columns in SEARCH_INDEXES.items():
            for column in columns:
                conn.execute(text(f'DROP INDEX IF EXISTS "{catalog.schema}"."idx_catalog_{table}_{column}_trgm" RESTRICT'))


def restore_search_indexes(catalog):
    """Build one index per bounded-memory transaction; retries are idempotent."""
    for table, columns in SEARCH_INDEXES.items():
        for column in columns:
            with catalog.engine.begin() as conn:
                conn.execute(text("SET LOCAL maintenance_work_mem='16MB'"))
                conn.execute(text('SET LOCAL max_parallel_maintenance_workers=0'))
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS "idx_catalog_{table}_{column}_trgm" '
                                  f'ON "{catalog.schema}"."catalog_{table}" USING gin ("{column}" gin_trgm_ops)'))
                valid = conn.execute(text('''SELECT i.indisvalid FROM pg_index i
                    WHERE i.indexrelid=to_regclass(:index)'''),
                    {'index': f'"{catalog.schema}"."idx_catalog_{table}_{column}_trgm"'}).scalar_one()
                if not valid:
                    raise ValueError('catalog search index is invalid; rebuild before resuming writers')
