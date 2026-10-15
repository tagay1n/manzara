# Publisher double quote cleanup

These commands are for the owner's PostgreSQL web SQL editor. They are not an
Alembic migration and are not executed by the application. Each code block is
one top-level statement, within the editor's ten-statement limit. Execute the
entire cleanup `DO ...` block together, including its dollar delimiters; its
internal statements must not be submitted separately. Use normal autocommit
mode, outside an existing transaction.

The selected private YAML currently uses schema `monocorpus`. If operating on a
different catalog, replace the schema in the preview and verification queries
and the `catalog_schema` variable in the cleanup block.

The cleanup removes `" « » “ ” „ ‟ 「 」 『 』 ＂` throughout publisher spellings,
then collapses whitespace and trims. Apostrophes and `[ ]` remain. It covers
publisher review rows, publisher credits, and aliases of publisher identities.
Publisher identity selection follows the catalog's publisher projection:
non-person entities, plus entities with an explicit publisher role or credit.
Canonical entity display names, other credit roles, and historical evidence,
proposals, and analysis snapshots are retained. Original name rows remain so
those references keep their original spelling.

Stop catalog-writing tasks and publisher clustering before running cleanup.
The block also takes the publisher analysis/review locks and catalog table
locks, with a short lock timeout. It aborts atomically on conflicting reviewed
decisions, collisions involving different identity links, empty cleaned names,
or ambiguous shared alias/review roles. The error identifies the affected
spellings, reviews, and links. No publisher identities are merged.

Compatible duplicate spellings share one cleaned name row of the same kind.
Alias links remain attached to their existing entities; duplicate links retain
the target alias ID when present, otherwise the lowest alias ID. Confirmation
is retained if any duplicate link was confirmed. Reviews must agree on status,
entity target, and cleaned name key (including pending reviews); otherwise the
block aborts. The existing target review wins, then the lowest review ID.
Its review metadata is retained, including its
counts (which are not summed because occurrences can overlap). Contribution
IDs, order, entity resolutions, and confirmation status remain unchanged.
Affected contributions, publications, surviving aliases, and linked entities
advance their revisions. After cleanup, generate a fresh publisher analysis
before reviewing proposals derived from an older inventory.

## Preview (read-only)

This lists every affected spelling, its cleaned form, any existing target name
ID, and its reviews and links. An empty cleaned spelling needs manual correction.
The whitespace expression matches Python's Unicode whitespace handling.

```sql
WITH publisher_entities AS (
    SELECT e.entity_id
    FROM monocorpus.catalog_entities e
    WHERE e.kind <> 'person'
       OR EXISTS (SELECT 1 FROM monocorpus.catalog_entity_roles r
                  WHERE r.entity_id = e.entity_id AND r.role = 'publisher')
       OR EXISTS (SELECT 1 FROM monocorpus.catalog_contributions c
                  WHERE c.entity_id = e.entity_id AND c.role = 'publisher')
), publisher_names AS (
    SELECT name_id FROM monocorpus.catalog_alias_reviews WHERE entity_type = 'publisher'
    UNION
    SELECT name_id FROM monocorpus.catalog_contributions WHERE role = 'publisher'
    UNION
    SELECT a.name_id FROM monocorpus.catalog_aliases a JOIN publisher_entities e USING (entity_id)
), affected AS (
    SELECT n.*, btrim(regexp_replace(translate(n.raw_name, '"«»“”„‟「」『』＂', ''),
        U&'[\0009-\000D\001C-\0020\0085\00A0\1680\2000-\200A\2028\2029\202F\205F\3000]+',
        ' ', 'g')) AS clean_name
    FROM monocorpus.catalog_names n JOIN publisher_names p USING (name_id)
    WHERE n.raw_name <> translate(n.raw_name, '"«»“”„‟「」『』＂', '')
       OR EXISTS (SELECT 1 FROM monocorpus.catalog_alias_reviews r
                  WHERE r.name_id = n.name_id AND r.entity_type = 'publisher'
                    AND r.normalized_name <> translate(r.normalized_name, '"«»“”„‟「」『』＂', ''))
)
SELECT s.name_id, s.kind, s.raw_name, s.clean_name, t.name_id AS existing_target_id,
    (SELECT jsonb_agg(jsonb_build_object('name_id', r.name_id, 'review_id', r.alias_id,
        'type', r.entity_type, 'status', r.decision_status, 'entity_id', r.entity_id,
        'normalized_name', r.normalized_name))
     FROM monocorpus.catalog_alias_reviews r
     WHERE r.name_id IN (s.name_id, t.name_id)) AS reviews,
    (SELECT jsonb_agg(jsonb_build_object('name_id', a.name_id, 'entity_id', a.entity_id,
        'approval', a.approval))
     FROM monocorpus.catalog_aliases a
     WHERE a.name_id IN (s.name_id, t.name_id)) AS links
FROM affected s
LEFT JOIN monocorpus.catalog_names t ON t.kind = s.kind AND t.raw_name = s.clean_name
ORDER BY s.kind, s.clean_name, s.name_id;
```

## Cleanup (one atomic statement)

The block rechecks conflicts under locks; it does not depend on preview results
remaining current. A failure rolls back all catalog changes from this statement.
Temporary working tables disappear at transaction end. The block can be rerun:
already cleaned publisher uses are no longer selected.

```sql
DO $publisher_quotes$
DECLARE
    catalog_schema text := 'monocorpus';
    quotes constant text := '"«»“”„‟「」『』＂';
    spaces constant text := U&'[\0009-\000D\001C-\0020\0085\00A0\1680\2000-\200A\2028\2029\202F\205F\3000]+';
    spelling record;
    identity_id bigint;
    target_id bigint;
    name_ids bigint[];
    entity_ids bigint[];
    survivor_id bigint;
    confirmed boolean;
    saved_review record;
    has_review boolean;
    problem text;
    details text;
    cleaned_groups bigint := 0;
BEGIN
    PERFORM set_config('search_path', format('%I, pg_catalog, public', catalog_schema), true);
    PERFORM set_config('lock_timeout', '5s', true);
    IF NOT pg_try_advisory_xact_lock(726193450) THEN
        RAISE EXCEPTION 'Publisher clustering is running; stop it before cleanup';
    END IF;
    IF NOT pg_try_advisory_xact_lock(hashtext('catalog-identity-review')) THEN
        RAISE EXCEPTION 'Identity review is running; retry cleanup after it finishes';
    END IF;
    LOCK TABLE catalog_entities, catalog_entity_roles, catalog_names,
        catalog_aliases, catalog_alias_reviews, catalog_contributions,
        catalog_publications IN SHARE ROW EXCLUSIVE MODE;

    CREATE TEMP TABLE publisher_quote_sources ON COMMIT DROP AS
    WITH publisher_entities AS (
        SELECT e.entity_id
        FROM catalog_entities e
        WHERE e.kind <> 'person'
           OR EXISTS (SELECT 1 FROM catalog_entity_roles r
                      WHERE r.entity_id = e.entity_id AND r.role = 'publisher')
           OR EXISTS (SELECT 1 FROM catalog_contributions c
                      WHERE c.entity_id = e.entity_id AND c.role = 'publisher')
    ), publisher_names AS (
        SELECT name_id FROM catalog_alias_reviews WHERE entity_type = 'publisher'
        UNION
        SELECT name_id FROM catalog_contributions WHERE role = 'publisher'
        UNION
        SELECT a.name_id FROM catalog_aliases a JOIN publisher_entities e USING (entity_id)
    )
    SELECT n.*, btrim(regexp_replace(translate(n.raw_name, quotes, ''), spaces, ' ', 'g')) AS clean_name
    FROM catalog_names n JOIN publisher_names p USING (name_id)
    WHERE n.raw_name <> translate(n.raw_name, quotes, '')
       OR EXISTS (SELECT 1 FROM catalog_alias_reviews r
                  WHERE r.name_id = n.name_id AND r.entity_type = 'publisher'
                    AND r.normalized_name <> translate(r.normalized_name, quotes, ''));

    FOR spelling IN
        SELECT DISTINCT kind, clean_name FROM publisher_quote_sources ORDER BY kind, clean_name
    LOOP
        SELECT n.name_id INTO target_id FROM catalog_names n
        WHERE n.kind = spelling.kind AND n.raw_name = spelling.clean_name;
        SELECT array_agg(DISTINCT ids.name_id ORDER BY ids.name_id) INTO name_ids
        FROM (
            SELECT s.name_id FROM publisher_quote_sources s
            WHERE s.kind = spelling.kind AND s.clean_name = spelling.clean_name
            UNION
            SELECT target_id WHERE target_id IS NOT NULL
        ) ids;
        SELECT jsonb_build_object(
            'names', (SELECT jsonb_agg(to_jsonb(n)) FROM catalog_names n WHERE n.name_id = ANY(name_ids)),
            'reviews', (SELECT jsonb_agg(to_jsonb(r)) FROM catalog_alias_reviews r WHERE r.name_id = ANY(name_ids)),
            'links', (SELECT jsonb_agg(to_jsonb(a)) FROM catalog_aliases a WHERE a.name_id = ANY(name_ids))
        )::text INTO details;

        problem := NULL;
        IF spelling.clean_name = '' THEN
            problem := 'publisher spelling becomes empty';
        ELSIF EXISTS (SELECT 1 FROM catalog_alias_reviews r
                      WHERE r.name_id = ANY(name_ids) AND r.entity_type <> 'publisher') THEN
            problem := 'spelling also has a non-publisher review';
        ELSIF EXISTS (
            SELECT 1 FROM catalog_aliases a JOIN catalog_entities e USING (entity_id)
            WHERE a.name_id = ANY(name_ids) AND e.kind = 'person'
              AND NOT EXISTS (SELECT 1 FROM catalog_entity_roles r
                              WHERE r.entity_id = e.entity_id AND r.role = 'publisher')
              AND NOT EXISTS (SELECT 1 FROM catalog_contributions c
                              WHERE c.entity_id = e.entity_id AND c.role = 'publisher')
        ) THEN
            problem := 'spelling also has a non-publisher identity alias';
        ELSIF EXISTS (
            SELECT 1 FROM catalog_alias_reviews r
            WHERE r.name_id = ANY(name_ids)
              AND btrim(regexp_replace(translate(r.normalized_name, quotes, ''), spaces, ' ', 'g')) = ''
        ) THEN
            problem := 'review spelling becomes empty';
        ELSIF (SELECT count(DISTINCT (r.decision_status, r.entity_id,
                    catalog_name_key(btrim(regexp_replace(translate(r.normalized_name, quotes, ''), spaces, ' ', 'g')))))
               FROM catalog_alias_reviews r
               WHERE r.name_id = ANY(name_ids)) > 1 THEN
            problem := 'incompatible reviewed decisions';
        ELSIF cardinality(name_ids) > 1 AND (
            SELECT count(DISTINCT links.entity_id) FROM (
                SELECT a.entity_id FROM catalog_aliases a WHERE a.name_id = ANY(name_ids)
                UNION
                SELECT r.entity_id FROM catalog_alias_reviews r
                WHERE r.name_id = ANY(name_ids) AND r.entity_id IS NOT NULL
            ) links
        ) > 1 THEN
            problem := 'colliding spellings have different identity links';
        END IF;
        IF problem IS NOT NULL THEN
            RAISE EXCEPTION 'Publisher quote cleanup aborted: % (clean spelling: %)', problem, spelling.clean_name
                USING DETAIL = details;
        END IF;

        -- Save the surviving review before alias triggers recompute review rows.
        SELECT r.* INTO saved_review FROM catalog_alias_reviews r
        WHERE r.name_id = ANY(name_ids) AND r.entity_type = 'publisher'
        ORDER BY (r.name_id = target_id) DESC NULLS LAST, r.alias_id
        LIMIT 1;
        has_review := FOUND;
        SELECT array_agg(DISTINCT linked.entity_id) INTO entity_ids FROM (
            SELECT a.entity_id FROM catalog_aliases a WHERE a.name_id = ANY(name_ids)
            UNION
            SELECT r.entity_id FROM catalog_alias_reviews r
            WHERE r.name_id = ANY(name_ids) AND r.entity_id IS NOT NULL
            UNION
            SELECT c.entity_id FROM catalog_contributions c
            WHERE c.name_id = ANY(name_ids) AND c.role = 'publisher' AND c.entity_id IS NOT NULL
        ) linked;

        IF target_id IS NULL THEN
            INSERT INTO catalog_names(kind, raw_name) VALUES (spelling.kind, spelling.clean_name)
            RETURNING name_id INTO target_id;
        END IF;

        FOR identity_id IN
            SELECT DISTINCT a.entity_id FROM catalog_aliases a
            WHERE a.name_id = ANY(name_ids) ORDER BY a.entity_id
        LOOP
            SELECT a.alias_id INTO survivor_id FROM catalog_aliases a
            WHERE a.name_id = ANY(name_ids) AND a.entity_id = identity_id
            ORDER BY (a.name_id = target_id) DESC, a.alias_id LIMIT 1;
            SELECT bool_or(a.approval = 'confirmed') INTO confirmed FROM catalog_aliases a
            WHERE a.name_id = ANY(name_ids) AND a.entity_id = identity_id;
            DELETE FROM catalog_aliases a
            WHERE a.name_id = ANY(name_ids) AND a.entity_id = identity_id AND a.alias_id <> survivor_id;
            UPDATE catalog_aliases SET name_id = target_id,
                approval = CASE WHEN confirmed THEN 'confirmed' ELSE 'unconfirmed' END,
                revision = revision + 1, updated_at = CURRENT_TIMESTAMP
            WHERE alias_id = survivor_id;
        END LOOP;

        WITH changed AS (
            UPDATE catalog_contributions SET name_id = target_id,
                revision = revision + 1, updated_at = CURRENT_TIMESTAMP
            WHERE role = 'publisher' AND name_id = ANY(name_ids) AND name_id <> target_id
            RETURNING publication_id
        )
        UPDATE catalog_publications SET revision = revision + 1, updated_at = CURRENT_TIMESTAMP
        WHERE publication_id IN (SELECT publication_id FROM changed);

        -- Remove obsolete spelling reviews after all alias triggers have run.
        DELETE FROM catalog_alias_reviews
        WHERE entity_type = 'publisher' AND name_id = ANY(name_ids) AND name_id <> target_id;
        IF has_review THEN
            DELETE FROM catalog_alias_reviews WHERE entity_type = 'publisher' AND name_id = target_id;
            INSERT INTO catalog_alias_reviews
            SELECT (jsonb_populate_record(NULL::catalog_alias_reviews,
                to_jsonb(saved_review) || jsonb_build_object(
                    'name_id', target_id,
                    'normalized_name', btrim(regexp_replace(translate(saved_review.normalized_name, quotes, ''), spaces, ' ', 'g')),
                    'updated_at', CURRENT_TIMESTAMP::text))).*;
        END IF;
        UPDATE catalog_entities SET revision = revision + 1, updated_at = CURRENT_TIMESTAMP
        WHERE entity_id = ANY(coalesce(entity_ids, ARRAY[]::bigint[]));
        cleaned_groups := cleaned_groups + 1;
    END LOOP;
    RAISE NOTICE 'Cleaned % publisher spelling groups', cleaned_groups;
END;
$publisher_quotes$;
```

## Verification (read-only)

Rerun the preview: it should return no rows. The query below independently counts
remaining quoted publisher credits and publisher review spellings; all counts
should be zero. Original unreferenced names, non-publisher uses, canonical
display names, and historical JSON can still contain quotation marks by design.
If cleanup raises an exception, correct the reported conflict before rerunning;
do not change its guards to force consolidation.

```sql
SELECT
    (SELECT count(*) FROM monocorpus.catalog_contributions c
     JOIN monocorpus.catalog_names n USING (name_id)
     WHERE c.role = 'publisher'
       AND n.raw_name <> translate(n.raw_name, '"«»“”„‟「」『』＂', '')) AS quoted_publisher_credits,
    (SELECT count(*) FROM monocorpus.catalog_alias_reviews r
     JOIN monocorpus.catalog_names n USING (name_id)
     WHERE r.entity_type = 'publisher'
       AND n.raw_name <> translate(n.raw_name, '"«»“”„‟「」『』＂', '')) AS quoted_publisher_reviews,
    (SELECT count(*) FROM monocorpus.catalog_alias_reviews r
     WHERE r.entity_type = 'publisher'
       AND r.normalized_name <> translate(r.normalized_name, '"«»“”„‟「」『』＂', '')) AS quoted_normalized_review_names;
```
