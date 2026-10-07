"""Offline SQL rendering for the resumable classification hierarchy batch."""

import json
import re

from app.catalog.schema import build_metadata
from scripts.catalog_manual_foundation import _foundation_models, _verify_models
from scripts.catalog_sql import _editor_block


def _taxonomy_rows(source):
    nodes, classifications, known = [], [], {}
    for row in source:
        parent = None
        path = json.loads(row['path_en']) if isinstance(row['path_en'], str) else row['path_en']
        translated = row.get('path_tt')
        translated = (json.loads(translated) if isinstance(translated, str) else translated) or []
        if not isinstance(path, list) or not path or any(not isinstance(label, str) or not label.strip() for label in path):
            raise ValueError('source classification path must be a nonempty text array')
        if not isinstance(translated, list) or any(label is not None and not isinstance(label, str) for label in translated):
            raise ValueError('translated classification path must be a text array')
        for position, label in enumerate(path):
            key = (row['ddc'], tuple(part.casefold() for part in path[:position + 1]))
            tt = translated[position] if position < len(translated) else None
            if key not in known:
                node = {'node_id': len(nodes) + 1, 'ddc': row['ddc'], 'parent_id': parent,
                        'label_en': label, 'label_tt': tt, 'source_id': row['id'], 'position': position,
                        'translation_source_id': row['id'] if tt is not None else None}
                known[key] = node
                nodes.append(node)
            elif tt is not None and known[key]['label_tt'] not in {None, tt}:
                raise ValueError('shared classification branch has conflicting translations')
            elif tt is not None:
                known[key]['label_tt'] = tt
                known[key]['translation_source_id'] = row['id']
            parent = known[key]['node_id']
        classifications.append({'classification_id': row['id'], 'node_id': parent,
                                'status': row.get('status', 'pending'), 'created_by': row.get('created_by', 'gemini'),
                                'created_at': row.get('created_at')})
    return nodes, classifications


def _mapping_entries(rows):
    payload = json.dumps(rows, separators=(',', ':')).replace("'", "''")
    return f"jsonb_array_elements('{payload}'::jsonb) AS entry(value)"


def render_manual_taxonomy(fingerprint, classifications, schema='monocorpus', dataset_schema='public',
                           *, classification_fingerprint):
    tables = sorted(table.name for table in build_metadata(schema).tables.values())
    metadata = build_metadata(dataset_schema)
    if not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
        raise ValueError('invalid reviewed fingerprint')
    if not re.fullmatch(r'[0-9a-f]{64}', classification_fingerprint):
        raise ValueError('invalid reviewed classification fingerprint')
    nodes, assignments = _taxonomy_rows(classifications)
    # Send only numeric provenance mappings. Text and timestamps stay in PostgreSQL.
    node_entries = _mapping_entries([[node[key] for key in ('node_id', 'parent_id', 'source_id', 'position',
                                                          'translation_source_id')] for node in nodes])
    assignment_entries = _mapping_entries([[row['classification_id'], row['node_id']] for row in assignments])
    projections = {
        'classification_nodes': f"""SELECT (value->>0)::bigint AS node_id,
            first_classification.ddc,(value->>1)::bigint AS parent_id,
            first_classification.path_en->>(value->>3)::integer AS label_en,
            translated_classification.path_tt->>(value->>3)::integer AS label_tt
            FROM {node_entries}
            JOIN "{dataset_schema}".classification first_classification ON first_classification.id=(value->>2)::bigint
            LEFT JOIN "{dataset_schema}".classification translated_classification ON translated_classification.id=(value->>4)::bigint""",
        'classifications': f"""SELECT original.id AS classification_id,(value->>1)::bigint AS node_id,
            original.status,original.created_by,original.created_at FROM {assignment_entries}
            JOIN "{dataset_schema}".classification original ON original.id=(value->>0)::bigint""",
    }
    models, preparations = {}, []
    for name, rows, key in [('classification_nodes', nodes, 'node_id'),
                            ('classifications', assignments, 'classification_id')]:
        columns = ','.join(f'"{column.name}"' for column in metadata.tables[f'{dataset_schema}.catalog_{name}'].columns
                           if column.name not in {'revision', 'updated_at'})
        expected = 'catalog_manual_expected_' + name
        preparations.append(f'CREATE TEMP TABLE {expected} ON COMMIT DROP AS {projections[name]};\n'
                            f"IF (SELECT count(*) FROM pg_temp.{expected})<>{len(rows)} THEN\n"
                            "    RAISE EXCEPTION 'taxonomy provenance mapping is incomplete';\nEND IF;")
        models[name] = {'columns': columns, 'key': key,
                        'query': f'SELECT {columns} FROM pg_temp.{expected}'}
    verification = _verify_models(schema, models, phase='taxonomy')
    foundation = _verify_models(schema, _foundation_models(schema))
    occupied = '\nUNION ALL '.join(f'SELECT 1 FROM "{schema}"."{name}"' for name in tables
                                  if name not in {'catalog_imports', 'catalog_collections', 'catalog_entities', 'catalog_entity_roles'})
    inserts = '\n'.join(f'INSERT INTO "{schema}".catalog_{name} ({model["columns"]})\n{model["query"]};'
                        for name, model in models.items())
    sequences = '\n'.join(f"""PERFORM setval(pg_get_serial_sequence('"{schema}".catalog_{name}','{model['key']}'),
    coalesce((SELECT max({model['key']}) FROM "{schema}".catalog_{name}),1),
    EXISTS(SELECT 1 FROM "{schema}".catalog_{name}));""" for name, model in models.items())
    body = f"""DECLARE
    migration "{schema}".catalog_imports;
BEGIN
    PERFORM set_config('lock_timeout','10s',true);
    PERFORM set_config('maintenance_work_mem','16MB',true);
    IF NOT pg_try_advisory_xact_lock(hashtext('catalog-import:{schema}')) THEN
        RAISE EXCEPTION 'another catalog migration is running';
    END IF;
    SELECT * INTO migration FROM "{schema}".catalog_imports
      WHERE source_fingerprint='{fingerprint}' FOR UPDATE;
    IF migration.import_id IS NULL OR migration.state<>'loading'
      OR migration.manifest->'manual_editor' IS DISTINCT FROM 'true'::jsonb
      OR migration.manifest->>'evidence_mode' IS DISTINCT FROM 'essential'
      OR migration.manifest->>'dataset_schema' IS DISTINCT FROM '{dataset_schema}' THEN
        RAISE EXCEPTION 'reviewed manual loading import required';
    END IF;
    LOCK TABLE {','.join(f'"{schema}"."{name}"' for name in tables)} IN SHARE ROW EXCLUSIVE MODE;
    LOCK TABLE "{dataset_schema}".classification,"{schema}".library_collections,
      "{schema}".normalization_canonicals,"{schema}".normalization_events IN SHARE ROW EXCLUSIVE MODE;
    IF migration.manifest->'source_tables'->'classification'->>'rows' IS DISTINCT FROM '{len(classifications)}' THEN
        RAISE EXCEPTION 'classification count differs from reviewed manifest';
    END IF;
    IF (SELECT encode(sha256(convert_to(coalesce(jsonb_agg(to_jsonb(c) ORDER BY id),'[]')::text,'UTF8')),'hex')
        FROM "{dataset_schema}".classification c) IS DISTINCT FROM '{classification_fingerprint}' THEN
        RAISE EXCEPTION 'reviewed classification source changed';
    END IF;
    {foundation}
    {chr(10).join(preparations)}
    IF migration.manifest->'manual_completed_steps' @> '["taxonomy"]'::jsonb THEN
        {verification}
        RETURN;
    END IF;
    IF migration.manifest->'manual_completed_steps' IS DISTINCT FROM '["initialized","foundation"]'::jsonb THEN
        RAISE EXCEPTION 'taxonomy batch is out of order';
    END IF;
    IF EXISTS({occupied}) THEN
        RAISE EXCEPTION 'unexpected catalog staging rows before taxonomy';
    END IF;
    DROP INDEX IF EXISTS "{schema}".idx_catalog_classification_nodes_label_en_trgm RESTRICT;
    DROP INDEX IF EXISTS "{schema}".idx_catalog_classification_nodes_label_tt_trgm RESTRICT;
    {inserts}
    {verification}
    {sequences}
    UPDATE "{schema}".catalog_imports SET manifest=jsonb_set(manifest,'{{manual_completed_steps}}',
      manifest->'manual_completed_steps'||'["taxonomy"]'::jsonb)
      WHERE import_id=migration.import_id;
END;"""
    return _editor_block(body)
