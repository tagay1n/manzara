"""Prepare guarded extraction/evaluation writes and submit one PostgreSQL batch."""

import json
import re
from copy import deepcopy
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

from app.catalog.command_batch import execute_catalog_batch
from app.catalog.contracts import CatalogConflict, boolean, document_md5, nonblank
from app.catalog.metadata import SCALARS, compose_metadata, decompose_metadata, items
from app.catalog.metadata_store import RELATIONAL_FIELDS, comparable_credits, credit_signature, unmanaged_subjects
from app.catalog.schema_org import is_english_facet, metadata_contract_issues
from app.document_operation_lock import DocumentOperationBusy
from app.runtime_config import config_integer

_BATCH = Path(__file__).with_suffix('.sql').read_text(encoding='utf-8')


def comparable_record(record):
    result = {key: deepcopy(value) for key, value in record.items() if key not in {'publication_id', 'revision'}}
    result['credits'] = comparable_credits(result['credits'])
    return result


def _projected_record(record):
    """Match the normalized readers' omission of nulls and empty author lists."""
    result = deepcopy(record)
    for field in ('subjects', 'audiences'):
        result[field] = [{key: value for key, value in item.items() if value is not None}
                         for item in result[field]]
    reference = result['based_on']
    if reference is not None:
        if set(reference) - {'@type', 'name', 'inLanguage', 'url', 'author'}:
            raise ValueError('unsupported source-work reference')
        normalized = {key: reference[key] for key in ('@type', 'name', 'inLanguage', 'url')
                      if key in reference and reference[key] is not None}
        authors = [{'@type': entity['@type'], 'name': entity['name']}
                   for entity in items(reference.get('author'))]
        if authors:
            normalized['author'] = authors
        result['based_on'] = normalized
    return result


def _decision(document, protected):
    if document['evaluation_method'] is None:
        return {}, {}
    changes = {field: value for field, value in {
        'inclusion': 'pending', 'evaluation_method': None,
    }.items() if document[field] != value}
    if protected & {'inclusion', 'evaluation_method'}:
        return {}, changes
    return changes, {}


def _merge(snapshot, incoming, *, reset_evaluation):
    source = snapshot['source']
    before = source['metadata_record']
    protected = set(source['publication_protections'])
    doc_protected = set(source['document_protections'])
    decision, decision_proposal = _decision(source, doc_protected) if reset_evaluation else ({}, {})
    prior = {(row['role'], row['position'], row['nested_position']): row for row in before['credits']}
    for credit in incoming['credits']:
        old = prior.get((credit['role'], credit['position'], credit['nested_position']))
        if (old and credit['kind'] == old['kind'] and credit['role_name'] == old['role_name']
                and credit['raw_name'] == old.get('display_name')):
            credit['raw_name'] = old['raw_name']
    changed = {field for field in RELATIONAL_FIELDS - {'credits'} if incoming[field] != before[field]}
    if comparable_credits(incoming['credits']) != comparable_credits(before['credits']):
        changed.add('credits')
        signatures = {credit_signature(row) for row in incoming['credits']}
        if any(row['resolution'] == 'confirmed' and credit_signature(row) not in signatures
               for row in before['credits']):
            protected.add('credits')
    scalars = {field: incoming['scalars'].get(field) for field in SCALARS.values()
               if snapshot['publication'][field] != incoming['scalars'].get(field)}
    if before['languages'] != incoming['languages']:
        scalars['languages'] = incoming['languages']
    blocked = {field: value for field, value in scalars.items() if field in protected}
    blocked.update({field: incoming[field] for field in changed & protected})
    if 'audiences' in blocked:
        blocked['audience_array'] = incoming['audience_array']
    file_changes = {field for field in {'access_modes', 'sufficient_modes'} if incoming[field] != before[field]}
    blocked.update({field: incoming[field] for field in file_changes & doc_protected})
    merged = deepcopy(before)
    for field, value in scalars.items():
        if field in protected:
            continue
        if field == 'languages':
            merged[field] = value
        elif value is None:
            merged['scalars'].pop(field, None)
        else:
            merged['scalars'][field] = value
    for field in changed - protected:
        merged[field] = incoming[field]
    if 'audiences' not in protected:
        merged['audience_array'] = incoming['audience_array']
    for field in file_changes - doc_protected:
        merged[field] = incoming[field]
    return merged, {
        'relations': sorted(changed - protected),
        'languages_changed': 'languages' in scalars and 'languages' not in protected,
        'file_changes': sorted(file_changes - doc_protected),
        'blocked': blocked, 'decision': decision, 'decision_proposal': decision_proposal,
    }


def _classified_schema(record, path):
    schema = compose_metadata(record)
    if path is not None:
        schema['about'] = unmanaged_subjects(record['subjects']) + [
            {'@type': 'DefinedTerm', 'termCode': value,
             'inDefinedTermSet': {'@type': 'DefinedTermSet', 'name': termset}}
            for termset, value in (('DDC', path['ddc']), ('CategoryPath', ' > '.join(path['path_en'])))
        ]
    return schema


def _evaluation(evaluation):
    if evaluation is None:
        return None
    applicable = boolean(evaluation['applicable'], 'applicable')
    reason = nonblank(evaluation['reason'], 'evaluation reason')
    ddc, path = evaluation['ddc'], evaluation['path']
    if applicable:
        if not isinstance(ddc, str) or not re.fullmatch(r'\d{3}(?:\.\d+)?', ddc):
            raise ValueError('Applicable evaluation requires a valid DDC')
        if not isinstance(path, list) or not 2 <= len(path) <= 8 or any(not is_english_facet(label) for label in path):
            raise ValueError('Applicable evaluation requires a valid category path')
    elif ddc is not None or path is not None:
        raise ValueError('Excluded evaluation requires null DDC and category path')
    return {'applicable': applicable, 'reason': reason, 'ddc': ddc, 'path': deepcopy(path)}


def _prepare(source, schema_org, *, actor, method, evaluation):
    if 'save_snapshot' not in source:
        raise CatalogConflict('Metadata save snapshot is missing; resume with a fresh batch')
    saved = source['save_snapshot']
    evaluation = _evaluation(evaluation)
    incoming = decompose_metadata(deepcopy(schema_org))
    incoming['subjects'] = unmanaged_subjects(incoming['subjects'])
    merged, plan = _merge(saved, incoming, reset_evaluation=evaluation is None)
    merged = _projected_record(merged)
    path = saved['source']['classification_path']
    validation = {}
    if evaluation is not None:
        # The server resolves taxonomy IDs and decides whether protections retain
        # the old decision. Validate both resulting envelopes before that request.
        accepted = {'ddc': evaluation['ddc'], 'path_en': evaluation['path']} if evaluation['applicable'] else path
        validation = {name: sorted({issue['code'] for issue in metadata_contract_issues(_classified_schema(merged, value))})
                      for name, value in (('accepted', accepted), ('retained', path))}
        decision_protected = bool(set(source['publication_protections']) & {'classification_id'}
                                  or set(source['document_protections']) & {'inclusion', 'evaluation_method'})
        issues = validation['accepted']
        review_possible = bool(plan['blocked']) or decision_protected
    else:
        issues = sorted({issue['code'] for issue in metadata_contract_issues(_classified_schema(merged, path))})
        review_possible = bool(plan['blocked'] or plan['decision_proposal'])
    if issues and not review_possible:
        raise CatalogConflict('Protected or retained metadata requires review: ' +
                              ', '.join(issues))
    prior = {credit_signature(row): row['contribution_id'] for row in saved['source']['metadata_record']['credits']}
    plan['retained_credits'] = [prior[credit_signature(row)] for row in merged['credits']
                               if credit_signature(row) in prior]
    return {
        'snapshot': saved, 'md5': document_md5(source['md5']),
        'actor': nonblank(actor, 'actor'), 'method': nonblank(method, 'method'),
        'schema_org': schema_org, 'expected': comparable_record(merged), 'plan': plan,
        'expected_classification_path': path, 'evaluation': evaluation, 'evaluation_validation': validation,
    }


def save_metadata_result(engine, schema, source, schema_org, *, actor, method, evaluation, snapshot_query):
    payload = _prepare(source, schema_org, actor=actor, method=method, evaluation=evaluation)
    try:
        return execute_catalog_batch(engine, schema, _BATCH.replace('{{snapshot_query}}', snapshot_query), {
            'payload': Json(payload, dumps=lambda value: json.dumps(value, ensure_ascii=False)),
            'lock_timeout': f"{config_integer('postgres', 'lock_timeout_seconds') * 1000}ms",
        }, expected_errors=('MZ001', 'MZ002'))
    except psycopg2.Error as exc:
        if exc.pgcode == 'MZ001':
            raise CatalogConflict(exc.diag.message_primary) from None
        if exc.pgcode == 'MZ002':
            raise DocumentOperationBusy(exc.diag.message_primary) from None
        raise
