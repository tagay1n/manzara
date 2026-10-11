WITH required AS (
    SELECT key AS relation, ARRAY(SELECT jsonb_array_elements_text(value)) AS columns
    FROM jsonb_each(CAST(:required AS jsonb))
), relations AS (
    SELECT r.*, c.oid
    FROM required r
    LEFT JOIN pg_namespace n ON n.nspname=:schema
    LEFT JOIN pg_class c ON c.relnamespace=n.oid AND c.relname=r.relation AND c.relkind='r'
), keys AS (
    SELECT * FROM jsonb_to_recordset(CAST(:keys AS jsonb))
        AS rule(relation text, kind text, columns text[])
), foreign_keys AS (
    SELECT * FROM jsonb_to_recordset(CAST(:foreign_keys AS jsonb))
        AS rule(relation text, parent text, column_name text)
), constraints AS (
    SELECT c.*, ARRAY(
        SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY k(number,position)
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.number ORDER BY k.position
    ) AS column_names
    FROM pg_constraint c JOIN relations r ON r.oid=c.conrelid
), deployed AS (
    SELECT version_num AS revision FROM "{{version_schema}}".alembic_version_manzara
), issues AS (
    SELECT 'Book previews require ' || r.relation || '; apply migrations separately' AS issue
        FROM relations r WHERE r.oid IS NULL
    UNION ALL
    SELECT 'Book previews require ' || r.relation || ' column ' || name
        FROM relations r CROSS JOIN LATERAL unnest(r.columns) AS needed(name)
        WHERE r.oid IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM pg_attribute a WHERE a.attrelid=r.oid AND a.attname=name AND NOT a.attisdropped)
    UNION ALL
    SELECT 'Book previews require ' || k.relation || ' key (' || array_to_string(k.columns, ', ') || ')'
        FROM keys k JOIN relations r USING(relation)
        WHERE NOT EXISTS (
            SELECT 1 FROM constraints c WHERE c.conrelid=r.oid AND c.contype::text=k.kind
                AND CASE WHEN k.kind='p' THEN c.column_names=k.columns
                    ELSE c.column_names @> k.columns AND k.columns @> c.column_names END)
    UNION ALL
    SELECT 'Book previews require cascading ownership for ' || f.relation
        FROM foreign_keys f JOIN relations r USING(relation)
        WHERE NOT EXISTS (
            SELECT 1 FROM constraints c JOIN pg_class parent ON parent.oid=c.confrelid
            JOIN pg_namespace n ON n.oid=parent.relnamespace
            WHERE c.conrelid=r.oid AND c.contype='f' AND c.confdeltype='c'
                AND c.column_names=ARRAY[f.column_name] AND parent.relname=f.parent AND n.nspname=:schema)
    UNION ALL
    SELECT 'Book previews require catalog revision 20261008_0062 or later'
        WHERE (SELECT count(*) FROM deployed)<>1 OR NOT EXISTS (
            SELECT 1 FROM deployed WHERE CASE WHEN revision ~ '^\d{8}_\d{4}$'
                THEN split_part(revision,'_',2)::integer>=62 ELSE false END)
), envelope AS (
    {{candidate_query}} LIMIT 0
)
SELECT COALESCE(array_agg(issue ORDER BY issue), ARRAY[]::text[])
FROM issues CROSS JOIN (SELECT count(*) FROM envelope) validated;
