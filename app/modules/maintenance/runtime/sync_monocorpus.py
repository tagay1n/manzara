"""Yandex discovery and resumable, guarded cleanup for daily maintenance."""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from boto3 import Session
from botocore.config import Config
from yadisk_client import YaDisk
from yadisk.exceptions import PathExistsError, PathNotFoundError

from app.catalog.contracts import document_md5, integer
from app.catalog.document_sync import SYNC_BATCH_SIZE, discovery_values
from app.document_cleanup_contracts import CLEANUP_PHASE_DATABASE, CLEANUP_PHASE_STORAGE, CLEANUP_PHASE_YANDEX
from app.document_cleanup_paths import cleanup_source_path, cleanup_target_path, source_path
from app.document_resources import EMPTY_MD5, cleanup_source_meta, resource_meta, resource_value, verify_resource
from app.document_storage import DocumentStorageSettings, load_document_storage_settings, remove_cached_document
from app.document_sync_filter import classify_document, normalize_document_mime
from app.document_operation_lock import DocumentOperationBusy, check_document_operation, document_operation
from app.modules.maintenance.document_cleanup_executor import execute_yandex_cleanup
from app.modules.maintenance.monocorpus_sync_repository import MonocorpusSyncRepository
from app.postgres_engine import is_transient_postgres_error
from app.runtime_config import load_runtime_config
from app.task_runtime.contracts import RunContext
from app.task_runtime.logging import redact

TASK_ID = 'maintenance.monocorpus_sync'
SYNC_FIELDS = ('mime_type', 'ya_path', 'ya_resource_id', 'ya_public_url', 'ya_public_key',
               'source_size', 'full', 'sharing_restricted')


def _is_restricted(path: str, settings: DocumentStorageSettings) -> bool:
    path, restricted = source_path(path), source_path(settings.restricted_path)
    return path == restricted or path.startswith(restricted + '/')


def _is_limited(path: str) -> bool:
    path = path.casefold()
    return '/limited/' in path and ('/milli_kitaphana/' in path or '/милли.китапханә/' in path)


def _walk_files(yadisk: Any, root: str, *, context: RunContext, counters: dict[str, int]) -> Iterable[dict[str, Any]]:
    root = source_path(root)
    stack = [root]
    visited = set()
    while stack and not context.should_stop():
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        context.log(f'sync directory listing start path={current}')
        _progress(context, counters, current)
        try:
            children = list(yadisk.listdir(current, fields=[
                'name', 'path', 'type', 'size', 'md5', 'mime_type', 'resource_id', 'public_key', 'public_url']))
        except PathNotFoundError as exc:
            if current == root:
                raise RuntimeError('Configured Yandex document source root is unavailable') from exc
            counters['failed'] += 1
            context.log(f'sync directory unavailable path={current} error={type(exc).__name__}')
            continue
        context.log(f'sync directory listing complete path={current} resources={len(children)}')
        for resource in reversed(children):
            if context.should_stop():
                return
            path = resource_value(resource, 'path')
            if not path:
                continue
            try:
                path = source_path(path)
            except ValueError as exc:
                counters['failed'] += 1
                # Escape rejected characters so the diagnostic stays on one
                # log line and identifies the resource requiring correction.
                rejected = json.dumps(path, ensure_ascii=True, default=str)
                context.log(f'sync resource rejected directory={current} path={rejected} error={exc}')
                _progress(context, counters, current)
                continue
            if path != root and not path.startswith(root + '/'):
                raise RuntimeError('Yandex returned a resource outside the configured source root')
            kind = resource_value(resource, 'type')
            if kind == 'dir':
                stack.append(path)
            elif kind == 'file':
                yield {
                    'source_path': path, 'source_size': resource_value(resource, 'size'),
                    'source_md5': str(resource_value(resource, 'md5') or '').lower(),
                    'mime_type': normalize_document_mime(path, str(resource_value(resource, 'mime_type') or '')),
                    'resource_id': resource_value(resource, 'resource_id'),
                    'public_url': resource_value(resource, 'public_url'),
                    'public_key': resource_value(resource, 'public_key'),
                }


def _s3_client(connection: Any) -> Any:
    return Session().client(
        's3', aws_access_key_id=connection.access_key_id, aws_secret_access_key=connection.secret_access_key,
        endpoint_url=connection.endpoint_url, region_name=connection.region_name,
        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'},
                      connect_timeout=10, read_timeout=30, retries={'max_attempts': 2}),
    )


def _ensure_yandex_directory(yadisk: Any, directory: str) -> None:
    current = ''
    for part in PurePosixPath(source_path(directory)).parts:
        if part == '/':
            continue
        current += '/' + part
        try:
            yadisk.mkdir(current)
        except PathExistsError:
            continue


def _managed_keys(s3: Any, bucket: str, prefix: str) -> tuple[str, ...]:
    parameters = {'Bucket': bucket, 'Prefix': prefix, 'MaxKeys': 1000}
    while True:
        page = s3.list_objects_v2(**parameters)
        keys = tuple(item['Key'] for item in page.get('Contents', [])
                     if item['Key'] == prefix or item['Key'].startswith((prefix + '.', prefix.rstrip('/') + '/')))
        if keys:
            return keys
        if not page.get('IsTruncated'):
            return ()
        token = page.get('NextContinuationToken')
        if not token or token == parameters.get('ContinuationToken'):
            raise RuntimeError('Managed S3 object pagination did not advance')
        parameters['ContinuationToken'] = token


def _delete_prefix(s3: Any, bucket: str, prefix: str) -> int:
    deleted = 0
    previous = ()
    while True:
        keys = _managed_keys(s3, bucket, prefix)
        if not keys:
            return deleted
        if keys == previous:
            raise RuntimeError(f'Managed S3 objects remain after deletion bucket={bucket} prefix={prefix}')
        previous = keys
        for key in keys:
            s3.delete_object(Bucket=bucket, Key=key)
            deleted += 1


def _cleanup_managed_storage(md5: str, *, primary_s3: Any, settings: DocumentStorageSettings) -> int:
    document_md5(md5)
    targets = {(settings.public_bucket, md5), (settings.private_bucket, md5)}
    targets.update((bucket, prefix) for bucket, prefix in (
        (settings.preview_bucket, md5 + '/'), (settings.content_bucket, md5),
        (settings.content_images_bucket, md5 + '/')) if bucket)
    return sum(_delete_prefix(primary_s3, bucket, prefix) for bucket, prefix in sorted(targets))


def _apply_remote_cleanup(item: Mapping[str, Any], *, yadisk: Any) -> None:
    source = cleanup_source_meta(yadisk, item)
    if source is not None:
        if item['action'] == 'move':
            _ensure_yandex_directory(yadisk, str(PurePosixPath(source_path(item['target_path'])).parent))
        execute_yandex_cleanup(item, yadisk=yadisk)
    if cleanup_source_meta(yadisk, item) is not None:
        raise RuntimeError('Original Yandex source remains after cleanup')
    if item['action'] == 'move':
        verify_resource(resource_meta(yadisk, item['target_path']), md5=item['md5'],
                        resource_id=item.get('source_resource_id'))
    elif item['action'] != 'delete':
        raise ValueError('Unsupported cleanup action')
    if item['scope'] == 'duplicate_resource':
        canonical = source_path(item['evidence_json'].get('canonical_path'))
        if canonical == source_path(item['source_path']):
            raise RuntimeError('Duplicate cleanup addresses its canonical resource')
        verify_resource(resource_meta(yadisk, canonical), md5=item['md5'])


def _apply_cleanup(item: Mapping[str, Any], *, repository: MonocorpusSyncRepository, yadisk: Any,
                   primary_s3: Any, settings: DocumentStorageSettings, context: RunContext) -> tuple[int, str]:
    try:
        with repository.cleanup_operation(item['cleanup_id']) as conn:
            return _apply_cleanup_owned(item, repository=repository, yadisk=yadisk,
                                        primary_s3=primary_s3, settings=settings, context=context,
                                        operation_connection=conn)
    except DocumentOperationBusy as exc:
        context.log(f"cleanup deferred cleanup_id={item['cleanup_id']} error={exc}")
        return 0, 'deferred'
    except Exception as exc:
        context.log(f"cleanup operation failed cleanup_id={item['cleanup_id']} error={redact(exc)}")
        if is_transient_postgres_error(exc):
            raise
        return 0, 'failed'


def _apply_cleanup_owned(item: Mapping[str, Any], *, repository: MonocorpusSyncRepository, yadisk: Any,
                         primary_s3: Any, settings: DocumentStorageSettings, context: RunContext,
                         operation_connection) -> tuple[int, str]:
    cleanup_id = integer(item['cleanup_id'], 'cleanup_id')
    removed = 0
    try:
        claimed = repository.claim_cleanup(cleanup_id, run_id=context.run_id)
        if claimed is None:
            context.log(f'cleanup skipped cleanup_id={cleanup_id} reason=plan is no longer active')
            return 0, 'canceled'
        item = claimed
        context.log(f"cleanup start cleanup_id={cleanup_id} md5={item['md5']} phase={item['phase']}")
        # Recheck completed remote work on resume, without repeating it.
        if item['phase'] == CLEANUP_PHASE_YANDEX:
            if item['action'] == 'move' and cleanup_source_meta(yadisk, item) is None and resource_meta(yadisk, item['target_path']) is None:
                repository.mark_cleanup_canceled(cleanup_id, 'Cleanup source and target are both missing')
                context.log(f'cleanup canceled cleanup_id={cleanup_id} reason=source and target missing')
                return 0, 'canceled'
            check_document_operation(operation_connection)
            _apply_remote_cleanup(item, yadisk=yadisk)
            if item['scope'] == 'document':
                repository.mark_cleanup_phase(cleanup_id, CLEANUP_PHASE_STORAGE)
                item['phase'] = CLEANUP_PHASE_STORAGE
        else:
            if cleanup_source_meta(yadisk, item) is not None:
                raise RuntimeError('Cleanup source reappeared after the remote phase; inspect the plan')
            if item['action'] == 'move':
                verify_resource(resource_meta(yadisk, item['target_path']), md5=item['md5'],
                                resource_id=item.get('source_resource_id'))
        if item['scope'] == 'document':
            if item['phase'] == CLEANUP_PHASE_STORAGE:
                repository.validate_cleanup_document(item)
                check_document_operation(operation_connection)
                removed = _cleanup_managed_storage(item['md5'], primary_s3=primary_s3, settings=settings)
                cache = remove_cached_document(settings.cache_path, item['md5'])
                context.log(f"cleanup storage complete cleanup_id={cleanup_id} md5={item['md5']} objects_removed={removed} cache_removed={len(cache)}")
                repository.mark_cleanup_phase(cleanup_id, CLEANUP_PHASE_DATABASE)
            check_document_operation(operation_connection)
            repository.delete_document_state(item['md5'], expected=item['evidence_json']['execution_document'])
        elif item['reason'] == 'filename_newlines':
            repository.mark_cleanup_phase(cleanup_id, CLEANUP_PHASE_DATABASE)
            repository.repair_filename_catalog(item)
        repository.mark_cleanup_completed(cleanup_id)
        context.log(f"cleanup success cleanup_id={cleanup_id} md5={item['md5']} objects_removed={removed}")
        return removed, 'completed'
    except Exception as exc:
        repository.mark_cleanup_failed(cleanup_id, redact(f'{type(exc).__name__}: {exc}'))
        context.log(f'cleanup failed cleanup_id={cleanup_id} error={type(exc).__name__}: {exc}')
        if is_transient_postgres_error(exc):
            raise
        return removed, 'failed'


def _execute_plan(payload: Mapping[str, Any], *, repository: MonocorpusSyncRepository, yadisk: Any,
                  primary_s3: Any, settings: DocumentStorageSettings, context: RunContext, counters: dict[str, int]):
    cleanup_id, created = repository.enqueue_cleanup(payload)
    if payload['scope'] == 'source_resource':
        counters['corrupted_plans_created' if created else 'corrupted_plans_reused'] += 1
    else:
        counters['duplicate_resources_queued'] += int(created)
    removed, outcome = _apply_cleanup({'cleanup_id': cleanup_id}, repository=repository, yadisk=yadisk,
                                     primary_s3=primary_s3, settings=settings, context=context)
    counters['objects_removed'] += removed
    counters[f'cleanups_{outcome}'] += 1
    counters['failed'] += int(outcome in {'failed', 'deferred'})


def _canonical_path(current: Mapping[str, Any] | None) -> str | None:
    try:
        return source_path((current or {}).get('ya_path'))
    except ValueError:
        return None


def _plan_resource(resource: dict[str, Any], *, existing: dict[str, dict[str, Any]], planned: dict[str, dict[str, Any]],
                   repository: MonocorpusSyncRepository, yadisk: Any, primary_s3: Any,
                   settings: DocumentStorageSettings, context: RunContext, counters: dict[str, int]) -> None:
    path = source_path(resource['source_path'])
    size = resource['source_size']
    if size is not None:
        integer(size, 'source_size', minimum=0)
    if size == 0:
        md5 = document_md5(resource['source_md5'] or EMPTY_MD5)
        counters['corrupted_zero_detected'] += 1
        _execute_plan({
            'scope': 'source_resource', 'action': 'move', 'reason': 'corrupted', 'md5': md5,
            'source_resource_id': resource['resource_id'], 'source_path': path,
            'target_path': cleanup_target_path(settings.filtered_out_path, reason='corrupted',
                                               source_root_path=settings.source_path, source_path=path),
            'evidence': {'detector': 'yandex_zero_byte', 'source_size': 0, 'mime_type': resource['mime_type'],
                         'run_id': context.run_id, 'task_id': TASK_ID},
        }, repository=repository, yadisk=yadisk, primary_s3=primary_s3, settings=settings, context=context, counters=counters)
        return
    current = existing.get(resource['source_md5'])
    verified_mime = (current or {}).get('verified_source_mime')
    mime = verified_mime or resource['mime_type']
    decision = classify_document(path, mime)
    if not decision.accepted:
        counters['filtered'] += 1
        context.log(f"sync filtered md5={resource['source_md5']} path={path} mime_type={decision.mime_type} reason={decision.reason}")
        return
    md5 = document_md5(resource['source_md5'])
    canonical = _canonical_path(current)
    if canonical and canonical != path:
        meta = resource_meta(yadisk, canonical)
        if meta is not None and str(resource_value(meta, 'md5') or '').lower() == md5:
            verify_resource(meta, md5=md5, resource_id=current.get('ya_resource_id'))
            _execute_plan({
                'scope': 'duplicate_resource', 'action': 'delete', 'reason': 'duplicate_md5', 'md5': md5,
                'source_resource_id': resource['resource_id'], 'source_path': path, 'target_path': None,
                'evidence': {'canonical_path': canonical},
            }, repository=repository, yadisk=yadisk, primary_s3=primary_s3, settings=settings, context=context, counters=counters)
            return
        context.log(f'sync restore source md5={md5} reason=canonical file missing or has different MD5')
    restricted = _is_restricted(path, settings) or (current is not None and current['sharing_restricted'] is not False)
    payload = {
        'md5': md5, 'mime_type': verified_mime or decision.mime_type, 'ya_path': path,
        'ya_resource_id': resource['resource_id'], 'ya_public_url': None if restricted else resource['public_url'],
        'ya_public_key': None if restricted else resource['public_key'], 'source_size': size,
        'full': not _is_limited(path), 'sharing_restricted': restricted,
    }
    discovery_values(payload)
    if current and all(current[key] == payload[key] for key in SYNC_FIELDS) and (restricted or payload['ya_public_url']):
        counters['unchanged'] += 1
        context.log(f"sync catalog unchanged md5={md5} publication_id={current['publication_id']} path={path}")
        return
    # Virtual sources retain canonical/strict privacy decisions for later copies
    # of the same MD5. Expected revisions always refer to the persisted snapshot.
    expected = planned[md5]['expected'] if md5 in planned else current
    planned[md5] = {'payload': payload, 'expected': expected}
    existing[md5] = {'publication_id': None, **(current or {}), **payload, 'verified_source_mime': verified_mime}
    counters['catalog_planned'] = len(planned)
    context.log(f'sync catalog planned md5={md5} path={path}')


def _record_batch_conflicts(batch, *, context, counters):
    for md5, error in batch['conflicts'].items():
        counters['failed'] += 1
        context.log(f'sync catalog conflict md5={md5} error={error}')


def _publish_batch(results, *, repository, yadisk, context, counters):
    links = []
    targets = [(md5, result) for md5, result in results.items()
               if not result['document']['sharing_restricted'] and not result['document']['ya_public_url']]
    counters['publications_pending'] += len(targets)
    for number, (md5, result) in enumerate(targets):
        current = result['document']
        if context.should_stop():
            break
        path = current['ya_path']
        _progress(context, counters, path, stage='publishing', current=number, total=len(targets))
        try:
            with document_operation(repository.engine, md5) as operation_connection:
                # The scan may be old: recheck catalog privacy/revisions and remote
                # identity immediately before each individual publication request.
                repository.validate_publication(current)
                verify_resource(resource_meta(yadisk, path), md5=md5, resource_id=current['ya_resource_id'])
                check_document_operation(operation_connection)
                yadisk.publish(path)
                published = resource_meta(yadisk, path)
                verify_resource(published, md5=md5, resource_id=current['ya_resource_id'])
                public_url = resource_value(published, 'public_url')
                if not public_url:
                    raise RuntimeError('Yandex publication did not return a public URL')
                counters['published'] += 1
                links.append({'payload': {**current, 'ya_public_url': public_url,
                                          'ya_public_key': resource_value(published, 'public_key')}, 'expected': current})
                context.log(f'sync publication success md5={md5} path={path}')
        except Exception as exc:
            if is_transient_postgres_error(exc):
                raise
            counters['failed'] += 1
            context.log(f'sync publication failed md5={md5} path={path} error={type(exc).__name__}: {exc}')
        _progress(context, counters, path, stage='publishing', current=number + 1, total=len(targets))
    # Flush completed remote publications even when a safe stop was requested.
    if links:
        batch = repository.save_discovered_documents(links)
        _record_batch_conflicts(batch, context=context, counters=counters)
        counters['publications_pending'] -= len(batch['results'])


def _apply_catalog(planned, *, repository, yadisk, context, counters):
    commands = list(planned.values())
    total = len(commands)
    _progress(context, counters, stage='applying', current=0, total=total)
    for start in range(0, total, SYNC_BATCH_SIZE):
        if context.should_stop():
            break
        batch = repository.save_discovered_documents(commands[start:start + SYNC_BATCH_SIZE])
        _record_batch_conflicts(batch, context=context, counters=counters)
        counters['catalog_applied'] += len(batch['results'])
        for md5, result in batch['results'].items():
            counters[result['outcome']] += 1
            current = result['document']
            context.log(f"sync catalog success md5={md5} publication_id={current['publication_id']} "
                        f"path={current['ya_path']} state={result['outcome']}")
        _progress(context, counters, stage='applying', current=min(start + SYNC_BATCH_SIZE, total), total=total)
        _publish_batch(batch['results'], repository=repository, yadisk=yadisk, context=context, counters=counters)
        _progress(context, counters, stage='applying', current=min(start + SYNC_BATCH_SIZE, total), total=total)
    counters['catalog_pending'] = total - counters['catalog_applied']


def _progress(context: RunContext, counters: Mapping[str, int], path: str = '', *, stage='scanning', **values):
    counters = {**counters, 'catalog_pending': counters['catalog_planned'] - counters['catalog_applied']}
    context.progress({'stage': stage, 'current_path': path, **dict(counters), **values}, force=stage == 'finished')


def run_monocorpus_sync(*, repository: MonocorpusSyncRepository, yadisk: Any, primary_s3: Any,
                        settings: DocumentStorageSettings, context: RunContext) -> dict[str, Any]:
    counters = dict.fromkeys((
        'discovered', 'created', 'updated', 'unchanged', 'published', 'duplicate_resources_queued',
        'corrupted_zero_detected', 'corrupted_plans_created', 'corrupted_plans_reused', 'filtered',
        'cleanups_completed', 'cleanups_canceled', 'cleanups_failed', 'cleanups_deferred', 'objects_removed', 'failed',
        'catalog_planned', 'catalog_applied', 'catalog_pending', 'publications_pending'), 0)
    error = None
    planned = {}
    context.log(f'sync start run_id={context.run_id}')
    try:
        context.log('sync loading active cleanup plans')
        items = repository.list_active_cleanup()
        context.log(f'sync cleanup queue loaded plans={len(items)}')
        for number, item in enumerate(items):
            if context.should_stop():
                break
            _progress(context, counters, item['source_path'], stage='cleanup', current=number, total=len(items))
            removed, outcome = _apply_cleanup(item, repository=repository, yadisk=yadisk, primary_s3=primary_s3,
                                             settings=settings, context=context)
            counters['objects_removed'] += removed
            counters[f'cleanups_{outcome}'] += 1
            counters['failed'] += int(outcome in {'failed', 'deferred'})
            _progress(context, counters, item['source_path'], stage='cleanup', current=number + 1, total=len(items))
        if context.should_stop():
            return _finish(context, counters, planned)
        # Do not rediscover files whose pending or failed cleanup still owns them.
        active = repository.list_active_cleanup()
        excluded_paths = {cleanup_source_path(item) for item in active}
        excluded_md5s = {item['md5'] for item in active if item['scope'] == 'document'}
        context.log('sync loading catalog document snapshot')
        existing = repository.list_documents()
        context.log(f'sync catalog snapshot loaded documents={len(existing)}')
        _progress(context, counters, stage='scanning')
        for resource in _walk_files(yadisk, settings.source_path, context=context, counters=counters):
            if context.should_stop():
                break
            counters['discovered'] += 1
            path = resource['source_path']
            context.log(f"sync file visited discovered={counters['discovered']} md5={resource['source_md5']} path={path}")
            _progress(context, counters, path)
            if path in excluded_paths or resource['source_md5'] in excluded_md5s:
                context.log(f'sync skipped path={path} reason=active cleanup plan')
                continue
            try:
                _plan_resource(resource, existing=existing, planned=planned, repository=repository, yadisk=yadisk,
                               primary_s3=primary_s3, settings=settings, context=context, counters=counters)
            except Exception as exc:
                if is_transient_postgres_error(exc):
                    raise
                counters['failed'] += 1
                context.log(f"sync item failed path={path} md5={resource['source_md5']} error={type(exc).__name__}: {exc}")
            _progress(context, counters, path)
        if not context.should_stop():
            context.log(f'sync discovery complete discovered={counters["discovered"]} catalog_planned={len(planned)}')
            _apply_catalog(planned, repository=repository, yadisk=yadisk, context=context, counters=counters)
    except Exception as exc:
        counters['failed'] += 1
        error = redact(f'{type(exc).__name__}: {exc}')
        context.log(f'sync aborted error={error}')
    return _finish(context, counters, planned, error=error)


def _finish(context, counters, planned, *, error=None):
    counters['catalog_pending'] = len(planned) - counters['catalog_applied']
    stopped = context.should_stop()
    outcome = 'failed' if counters['failed'] else 'stopped' if stopped else 'completed'
    summary = {'kind': 'maintenance.monocorpus_sync_summary', **counters, 'stopped': stopped, 'outcome': outcome}
    if error:
        summary['error'] = error
    _progress(context, counters, stage='finished')
    context.log(f'sync final {json.dumps(summary, sort_keys=True)}')
    return summary


def execute(context: RunContext) -> dict[str, Any]:
    if context.options.workers != 1 or context.options.limit is not None:
        raise ValueError('Sync requires one worker and no limit')
    if context.should_stop():
        return {'kind': 'maintenance.monocorpus_sync_summary', 'stopped': True, 'outcome': 'stopped'}
    settings = load_document_storage_settings(load_runtime_config())
    source_path(settings.source_path)
    source_path(settings.restricted_path)
    cleanup_target_path(settings.filtered_out_path, reason='corrupted',
                        source_root_path=settings.source_path, source_path=settings.source_path.rstrip('/') + '/file')
    with ExitStack() as resources:
        repository = MonocorpusSyncRepository(context.db.database_url, schema=context.db.schema)
        resources.callback(repository.dispose)
        context.log('sync setup catalog preflight start')
        repository.catalog.preflight()
        context.log('sync setup acquiring catalog lock')
        with repository.sync_lock():
            context.log('sync setup catalog lock acquired; validating Yandex token')
            yadisk = YaDisk(settings.yadisk_token)
            resources.callback(yadisk.close)
            yadisk.default_args.update(timeout=(10, 30), poll_timeout=60, n_retries=2)
            if yadisk.check_token() is not True:
                raise RuntimeError('Yandex Disk token validation failed')
            context.log('sync setup Yandex token valid; initializing primary storage client')
            primary_s3 = _s3_client(settings.primary)
            resources.callback(primary_s3.close)
            return run_monocorpus_sync(repository=repository, yadisk=yadisk, primary_s3=primary_s3,
                                      settings=settings, context=context)
