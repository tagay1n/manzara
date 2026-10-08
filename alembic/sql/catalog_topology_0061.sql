-- Acquire topology locks before DML obtains row locks, then check each edge.
CREATE FUNCTION "__CATALOG_SCHEMA__".catalog_lock_topology()
RETURNS TRIGGER LANGUAGE plpgsql AS $fn$
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('catalog-topology:'||TG_TABLE_SCHEMA||':'||TG_TABLE_NAME,0));
 RETURN NULL;
END $fn$;
CREATE FUNCTION "__CATALOG_SCHEMA__".catalog_validate_topology()
RETURNS TRIGGER LANGUAGE plpgsql AS $fn$
DECLARE identity BIGINT; target BIGINT; visited BIGINT[]; relation TEXT; edge TEXT;
BEGIN
 identity=(to_jsonb(NEW)->>TG_ARGV[0])::bigint;
 edge=TG_ARGV[1];target=(to_jsonb(NEW)->>edge)::bigint;visited=ARRAY[identity];
 relation=format('%I.%I',TG_TABLE_SCHEMA,TG_TABLE_NAME);
 IF TG_TABLE_NAME='catalog_classification_nodes' THEN
   IF EXISTS(SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_classification_nodes
     WHERE node_id=target AND ddc<>NEW.ddc) OR EXISTS(
     SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_classification_nodes WHERE parent_id=identity AND ddc<>NEW.ddc) THEN
     RAISE EXCEPTION 'classification parent and children must use the same DDC';
   END IF;
 END IF;
 WHILE target IS NOT NULL LOOP
   IF target=ANY(visited) THEN RAISE EXCEPTION 'catalog % contains a cycle',TG_TABLE_NAME; END IF;
   visited=array_append(visited,target);
   EXECUTE format('SELECT %I FROM %s WHERE %I=$1',edge,relation,TG_ARGV[0]) INTO target USING target;
 END LOOP;
 RETURN NEW;
END $fn$;

CREATE TRIGGER catalog_lock_node_topology BEFORE INSERT OR UPDATE OF parent_id,ddc OR DELETE
 ON "__CATALOG_SCHEMA__".catalog_classification_nodes FOR EACH STATEMENT EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_lock_topology();
CREATE TRIGGER catalog_validate_node_topology BEFORE INSERT OR UPDATE OF parent_id,ddc
 ON "__CATALOG_SCHEMA__".catalog_classification_nodes FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_validate_topology('node_id','parent_id');
CREATE TRIGGER catalog_lock_entity_topology BEFORE INSERT OR UPDATE OF merged_into_id OR DELETE
 ON "__CATALOG_SCHEMA__".catalog_entities FOR EACH STATEMENT EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_lock_topology();
CREATE TRIGGER catalog_validate_entity_topology BEFORE INSERT OR UPDATE OF merged_into_id
 ON "__CATALOG_SCHEMA__".catalog_entities FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_validate_topology('entity_id','merged_into_id');
CREATE TRIGGER catalog_lock_publication_topology BEFORE INSERT OR UPDATE OF merged_into_id OR DELETE
 ON "__CATALOG_SCHEMA__".catalog_publications FOR EACH STATEMENT EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_lock_topology();
CREATE TRIGGER catalog_validate_publication_topology BEFORE INSERT OR UPDATE OF merged_into_id
 ON "__CATALOG_SCHEMA__".catalog_publications FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_validate_topology('publication_id','merged_into_id');

-- Validate existing edges rather than assuming the previous backend prevented
-- every cycle. The visited array terminates traversal even on corrupt graphs.
DO $existing$
DECLARE item RECORD; cyclic BOOLEAN;
BEGIN
 FOR item IN SELECT * FROM (VALUES
   ('catalog_classification_nodes','node_id','parent_id'),
   ('catalog_entities','entity_id','merged_into_id'),
   ('catalog_publications','publication_id','merged_into_id')
 ) AS graphs(table_name,identity_column,edge_column) LOOP
   EXECUTE format('WITH RECURSIVE walk AS (
     SELECT %1$I AS id,%2$I AS target,ARRAY[%1$I] AS visited,false AS cycle FROM %3$I.%4$I WHERE %2$I IS NOT NULL
     UNION ALL SELECT n.%1$I,n.%2$I,w.visited||n.%1$I,n.%1$I=ANY(w.visited)
     FROM walk w JOIN %3$I.%4$I n ON n.%1$I=w.target WHERE NOT w.cycle
   ) SELECT EXISTS(SELECT 1 FROM walk WHERE cycle)',item.identity_column,item.edge_column,'__CATALOG_SCHEMA__',item.table_name)
   INTO cyclic;
   IF cyclic THEN RAISE EXCEPTION 'existing cycle in %; review before migration',item.table_name; END IF;
 END LOOP;
 IF EXISTS(SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_classification_nodes n
   JOIN "__CATALOG_SCHEMA__".catalog_classification_nodes p ON p.node_id=n.parent_id WHERE n.ddc<>p.ddc) THEN
   RAISE EXCEPTION 'existing parent-child DDC conflict; review before migration';
 END IF;
END $existing$;

CREATE FUNCTION "__CATALOG_SCHEMA__".catalog_validate_reference_urls()
RETURNS TRIGGER LANGUAGE plpgsql AS $fn$
DECLARE present BOOLEAN;
BEGIN
 IF TG_TABLE_NAME='catalog_reference_urls' THEN
   SELECT urls_present INTO present FROM "__CATALOG_SCHEMA__".catalog_references
     WHERE publication_id=NEW.publication_id FOR UPDATE;
   IF present IS FALSE THEN RAISE EXCEPTION 'reference URL list must be present before inserting URLs'; END IF;
 ELSIF NOT NEW.urls_present AND EXISTS(SELECT 1 FROM "__CATALOG_SCHEMA__".catalog_reference_urls
   WHERE publication_id=NEW.publication_id) THEN
   RAISE EXCEPTION 'delete reference URLs before marking their list absent';
 END IF;
 RETURN NEW;
END $fn$;
CREATE TRIGGER catalog_validate_reference_url BEFORE INSERT OR UPDATE ON "__CATALOG_SCHEMA__".catalog_reference_urls
 FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_validate_reference_urls();
CREATE TRIGGER catalog_validate_reference_url_presence BEFORE UPDATE OF urls_present ON "__CATALOG_SCHEMA__".catalog_references
 FOR EACH ROW EXECUTE FUNCTION "__CATALOG_SCHEMA__".catalog_validate_reference_urls();
