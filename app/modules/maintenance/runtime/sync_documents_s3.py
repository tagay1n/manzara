"""Workflow-only sequential transfer into verified primary Backblaze storage."""

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import threading
from typing import Any, Mapping

from boto3 import Session
from botocore.config import Config
from botocore.exceptions import ClientError
from yadisk_client import YaDisk

from app.artifacts import workspace_dir
from app.catalog.contracts import CatalogConflict, document_md5, integer
from app.catalog.document_transfer import pending
from app.document_cleanup_paths import source_path
from app.document_operation_lock import DocumentOperationBusy, check_document_operation, document_operation
from app.document_resources import resource_meta, resource_value, verify_resource
from app.document_storage import (
    DocumentStorageSettings, build_cache_index, calculate_md5, document_object_key,
    find_valid_cache_entry, load_document_storage_settings, object_url,
    resolve_document_object_location,
)
from app.modules.maintenance.document_sync_repository import PostgresDocumentSyncRepository
from app.modules.runtime_shared_utils import encrypt
from app.runtime_config import load_runtime_config
from app.task_runtime.contracts import RunContext
from app.task_runtime.logging import redact

TASK_ID = "maintenance.sync_documents_s3"
_DISK_RESERVE_BYTES = 1024**3


class YandexDownloadUnavailable(RuntimeError):
    """A pending document's persisted source could not be acquired."""


def _etag(value: Any) -> str:
    return str(value or "").strip().strip('"')


def _head_object_or_none(s3: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return dict(s3.head_object(Bucket=bucket, Key=key))
    except KeyError:
        return None
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def _remote_matches(
    remote: Mapping[str, Any] | None,
    *,
    md5: str,
    size: int | None,
) -> bool:
    if (not remote or size is None or type(remote.get("ContentLength")) is not int
            or remote['ContentLength'] != size):
        return False
    metadata = remote.get("Metadata")
    source_md5 = (
        str(metadata.get("source-md5") or "").lower()
        if isinstance(metadata, Mapping)
        else ""
    )
    return source_md5 == md5 or _etag(remote.get("ETag")).lower() == md5


def _confirm_upload(
    s3: Any,
    bucket: str,
    key: str,
    md5: str,
    size: int,
) -> dict[str, Any]:
    head = dict(s3.head_object(Bucket=bucket, Key=key))
    metadata = head.get("Metadata")
    if type(head.get("ContentLength")) is not int or head['ContentLength'] != size:
        raise RuntimeError("S3 size verification failed")
    if not isinstance(metadata, Mapping) or str(
        metadata.get("source-md5") or ""
    ).lower() != md5:
        raise RuntimeError("S3 MD5 metadata verification failed")
    return head


def _abort_incomplete_uploads(s3: Any, bucket: str, key: str, log) -> int:
    aborted = 0
    key_marker: str | None = None
    upload_id_marker: str | None = None
    while True:
        request: dict[str, Any] = {"Bucket": bucket, "Prefix": key}
        if key_marker:
            request["KeyMarker"] = key_marker
        if upload_id_marker:
            request["UploadIdMarker"] = upload_id_marker
        response = s3.list_multipart_uploads(**request)
        for upload in response.get("Uploads", []):
            upload_key = str(upload.get("Key") or "")
            upload_id = str(upload.get("UploadId") or "")
            if upload_key != key or not upload_id:
                continue
            s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
            aborted += 1
            log(
                "document upload: aborted incomplete multipart upload "
                f"bucket={bucket} key={key} upload_id={upload_id}",
            )
        truncated = response.get('IsTruncated', False)
        if type(truncated) is not bool:
            raise ValueError('Multipart pagination requires an explicit boolean IsTruncated')
        if not truncated:
            break
        key_marker = str(response.get("NextKeyMarker") or "") or None
        upload_id_marker = str(response.get("NextUploadIdMarker") or "") or None
        if not key_marker:
            raise RuntimeError('Incomplete multipart pagination response; retry exact-key recovery')
    return aborted


def _public_object_removed(s3: Any, bucket: str, key: str) -> bool:
    return _head_object_or_none(s3, bucket, key) is None


def _target(row, settings):
    md5 = document_md5(row['md5'])
    restricted = row['sharing_restricted']
    if type(restricted) is not bool:
        raise CatalogConflict('Document privacy is unknown')
    bucket = settings.private_bucket if restricted else settings.public_bucket
    path = source_path(row['ya_path']) if row.get('ya_path') else None
    restricted_root = source_path(settings.restricted_path)
    if path and (path == restricted_root or path.startswith(restricted_root + '/')) and not restricted:
        raise CatalogConflict('Restricted-folder source has unrestricted catalog privacy; refresh Sync before transfer')
    locator = str(row.get('document_url') or '').strip()
    if not locator:
        return bucket, document_object_key(md5, row.get('ya_path') or '', row.get('mime_type'))
    if restricted and not locator.startswith('enc:'):
        raise CatalogConflict('Restricted primary locator must be encrypted; review before repair')
    location = resolve_document_object_location(document_url=locator,
        encryption_key=settings.encryption_key, endpoint_url=settings.primary.endpoint_url)
    if (location is None or location[0] != bucket
            or document_object_key(md5, location[1], None) != location[1]):
        raise CatalogConflict('Primary locator is outside its document identity/privacy destination; review before repair')
    return location


@contextmanager
def _acquire_source(row, *, cache_index, yadisk, workspace, log):
    cached = find_valid_cache_entry(cache_index, row['md5'])
    if cached:
        size = cached[0].stat().st_size
        if row['source_size'] is not None and size != row['source_size']:
            raise RuntimeError('Cached document size differs from the catalog source')
        yield cached[0], 'cache'
        return
    path = row.get('ya_path')
    if not path:
        raise YandexDownloadUnavailable('Persisted Yandex source path is missing; refresh catalog discovery')
    meta = resource_meta(yadisk, path)
    verify_resource(meta, md5=row['md5'], resource_id=row['ya_resource_id'])
    size = integer(resource_value(meta, 'size'), 'Yandex source size', minimum=0)
    if row['source_size'] is not None and size != row['source_size']:
        raise CatalogConflict('Yandex source size changed; refresh catalog discovery')
    if shutil.disk_usage(workspace).free < size + _DISK_RESERVE_BYTES:
        raise RuntimeError(f'Insufficient runner disk space for document bytes={size}; retry on a larger runner')
    with TemporaryDirectory(prefix=row['md5'] + '-', dir=workspace) as directory:
        destination = Path(directory) / 'document.download'
        log(f"document download start md5={row['md5']} size={size}")
        try:
            yadisk.download(path, str(destination))
        except Exception as exc:
            raise YandexDownloadUnavailable(f'Yandex download failed: {type(exc).__name__}: {exc}') from exc
        if destination.stat().st_size != size or calculate_md5(destination) != row['md5']:
            raise RuntimeError('Downloaded source failed MD5/size verification')
        yield destination, 'yandex'


def _progress(context, counters, *, stage, total, processed, md5='', **values):
    size = values.get('current_size', 0)
    current_bytes = max(0, values.get('current_bytes', 0))
    fraction = min(current_bytes, size) / size if size > 0 else 0
    percent = min(100, round((processed + fraction) / total * 100, 2)) if total else 100
    values['current_bytes'] = current_bytes
    context.progress({'stage': stage, 'current': processed, 'total': total,
                      'percent': percent, 'current_path': '', 'current_bytes': 0,
                      'current_size': 0, 'current_md5': md5, **counters, **values})


def _cleanup_stale_upload(repository, conn, row, settings, s3, bucket, key, counters, log):
    # Only an object absent before this attempt can belong to this attempt.
    # Never delete a now-checkpointed object, even if our commit response was lost.
    check_document_operation(conn)
    with conn.begin():
        current = repository.snapshot(conn, row['md5'])
    if current and current['document_url']:
        location = resolve_document_object_location(document_url=current['document_url'],
            encryption_key=settings.encryption_key, endpoint_url=settings.primary.endpoint_url)
        if location == (bucket, key):
            log(f"stale upload retained md5={row['md5']} reason=current checkpoint uses object")
            return
    check_document_operation(conn)
    s3.delete_object(Bucket=bucket, Key=key)
    if not _public_object_removed(s3, bucket, key):
        raise RuntimeError('Stale attempt object remains after deletion')
    counters['stale_upload_cleaned'] += 1


def _upload_source(source, *, s3, bucket, key, row, size, context,
                   counters, total, processed):
    upload_bytes = 0
    progress_lock = threading.Lock()

    def on_upload(delta):
        nonlocal upload_bytes
        with progress_lock:
            upload_bytes += delta
            _progress(context, counters, stage='uploading', total=total,
                      processed=processed, md5=row['md5'],
                      current_bytes=upload_bytes, current_size=size)

    context.log(f"document upload start md5={row['md5']} size={size}")
    s3.upload_file(str(source), bucket, key,
        ExtraArgs={'Metadata': {'source-md5': row['md5']},
                   'ContentType': row.get('mime_type') or 'application/octet-stream'},
        Callback=on_upload)
    return _confirm_upload(s3, bucket, key, row['md5'], size)


def _transfer_one(row, *, repository, conn, settings, yadisk, s3, cache_index,
                  workspace, context, counters, total, processed, resources):
    row = repository.revalidate(conn, row)
    bucket, key = _target(row, settings)
    remote = _head_object_or_none(s3, bucket, key)
    created = False
    confirmed = False
    size = row['source_size']
    try:
        if not _remote_matches(remote, md5=row['md5'], size=size):
            _progress(context, counters, stage='downloading', total=total,
                      processed=processed, md5=row['md5'])
            source, kind = resources.enter_context(_acquire_source(
                row, cache_index=cache_index, yadisk=yadisk, workspace=workspace, log=context.log))
            counters[f'source_{kind}'] += 1
            size = source.stat().st_size
            repository.revalidate(conn, row)
            if not _remote_matches(remote, md5=row['md5'], size=size):
                _abort_incomplete_uploads(s3, bucket, key, context.log)
                repository.revalidate(conn, row)
                head = _upload_source(source, s3=s3, bucket=bucket, key=key, row=row,
                                      size=size, context=context, counters=counters,
                                      total=total, processed=processed)
                created = remote is None
                remote = head
                confirmed = True
                counters['uploaded'] += 1
                counters['reuploaded'] += int(not created)
                counters['bytes_uploaded'] += size
            else:
                counters['recovered_existing'] += 1
        else:
            counters['recovered_existing'] += 1
        repository.revalidate(conn, row)
        if row['sharing_restricted']:
            public_remote = _head_object_or_none(s3, settings.public_bucket, key)
            if public_remote is not None:
                # Refuse to delete a foreign/corrupt object based solely on its name.
                if not _remote_matches(public_remote, md5=row['md5'], size=size):
                    raise CatalogConflict('Public object identity is insufficient for restricted-object cleanup')
                repository.revalidate(conn, row)
                s3.delete_object(Bucket=settings.public_bucket, Key=key)
                if not _public_object_removed(s3, settings.public_bucket, key):
                    raise RuntimeError('Obsolete public object remains after deletion')
                counters['private_cleaned'] += 1
        locator = row['document_url']
        if not str(locator or '').strip():
            locator = object_url(settings.primary.endpoint_url, bucket, key)
            if row['sharing_restricted']:
                locator = encrypt(locator, {'encryption_key': settings.encryption_key})
        payload = {'locator': locator, 'size': integer(remote['ContentLength'], 'remote size', minimum=0),
                   'etag': _etag(remote.get('ETag')), 'verified_at': datetime.now(timezone.utc)}
        if not payload['etag']:
            raise RuntimeError('Confirmed object is missing an ETag')
        repository.save_storage_checkpoint(conn, row, payload)
        counters['checkpointed'] += 1
        counters['repaired'] += int(bool(str(row.get('document_url') or '').strip()))
        context.log(f"document checkpoint success md5={row['md5']}")
    except CatalogConflict:
        counters['checkpoint_raced'] += 1
        if created and confirmed:
            _cleanup_stale_upload(repository, conn, row, settings, s3, bucket, key, counters, context.log)
        raise


def run_document_upload(*, repository, yadisk, primary_s3, settings, context):
    counters = dict.fromkeys(('uploaded', 'reuploaded', 'recovered_existing', 'checkpointed', 'repaired',
                             'checkpoint_raced', 'stale_upload_cleaned', 'source_cache', 'source_yandex',
                             'skipped_download', 'private_cleaned', 'deferred', 'failed', 'bytes_uploaded'), 0)
    total = repository.count_pending_documents()
    context.log(f'document transfer start pending={total}')
    if not total:
        _progress(context, counters, stage='completed', total=0, processed=0)
        return {'kind': 'maintenance.document_s3_upload_summary', 'outcome': 'completed',
                'pending_before': 0, 'pending_after': 0, 'processed': 0, 'stopped': False, **counters}
    cache_index = build_cache_index(settings.cache_path)
    workspace = workspace_dir('maintenance', 'document-transfer', run_id=context.run_id)
    workspace.chmod(0o700)
    processed, cursor = 0, ''
    while not context.should_stop():
        rows = repository.list_pending_documents(after=cursor)
        if not rows:
            break
        for row in rows:
            if context.should_stop():
                break
            cursor = row['md5']
            result = {'kind': 'maintenance.document_s3_transfer_item', 'md5': row['md5'],
                      'outcome': 'completed'}
            context.log(f"document transfer process md5={row['md5']}")
            try:
                with document_operation(repository.engine, row['md5']) as conn, ExitStack() as resources:
                    # A candidate may have been completed by another run since paging.
                    with conn.begin():
                        current = repository.snapshot(conn, row['md5'])
                    if current is not None and not pending(current):
                        result['outcome'] = 'already_completed'
                        context.log(f"document transfer already completed md5={row['md5']}")
                    else:
                        _transfer_one(row, repository=repository, conn=conn, settings=settings,
                            yadisk=yadisk, s3=primary_s3, cache_index=cache_index, workspace=workspace,
                            context=context, counters=counters, total=total, processed=processed, resources=resources)
            except DocumentOperationBusy as exc:
                counters['deferred'] += 1
                result.update(outcome='deferred', error=redact(exc))
                context.log(f"document transfer deferred md5={row['md5']} error={exc}")
            except YandexDownloadUnavailable as exc:
                counters['skipped_download'] += 1
                result.update(outcome='source_unavailable', error=redact(exc))
                context.log(f"document source unavailable md5={row['md5']} error={exc}", level='ERROR')
            except Exception as exc:
                counters['failed'] += 1
                result.update(outcome='failed', error=redact(exc))
                context.log(f"document transfer failed md5={row['md5']} error={type(exc).__name__}: {exc}", level='ERROR')
            context.artifact(result)
            processed += 1
            _progress(context, counters, stage='transferring', total=total, processed=processed)
    stopped = context.should_stop()
    unresolved = counters['failed'] + counters['deferred'] + counters['skipped_download']
    outcome = 'failed' if unresolved else 'stopped' if stopped else 'completed'
    summary = {'kind': 'maintenance.document_s3_upload_summary', 'outcome': outcome,
               'pending_before': total, 'pending_after': repository.count_pending_documents(),
               'processed': processed, 'stopped': stopped, **counters}
    _progress(context, counters, stage=outcome, total=total, processed=processed)
    context.log(f'document transfer final {json.dumps(summary, sort_keys=True)}')
    return summary


def _create_s3_client(connection: Any) -> Any:
    return Session().client(
        "s3",
        aws_access_key_id=connection.access_key_id,
        aws_secret_access_key=connection.secret_access_key,
        endpoint_url=connection.endpoint_url,
        region_name=connection.region_name,
        config=Config(
            signature_version="s3v4",
            connect_timeout=10, read_timeout=60,
            retries={"mode": "standard", "total_max_attempts": 3},
            s3={"addressing_style": "path"},
        ),
    )


def _allows_public_read(acl: Mapping[str, Any]) -> bool:
    for grant in acl.get("Grants", []):
        if not isinstance(grant, Mapping):
            continue
        grantee = grant.get("Grantee")
        if not isinstance(grantee, Mapping):
            continue
        if (
            str(grantee.get("URI") or "").endswith("/AllUsers")
            and str(grant.get("Permission") or "") in {"READ", "FULL_CONTROL"}
        ):
            return True
    return False


def _validate_primary_buckets(s3: Any, public_bucket: str, private_bucket: str) -> None:
    if public_bucket == private_bucket:
        raise RuntimeError("Document public and private buckets must be different")
    s3.head_bucket(Bucket=public_bucket)
    s3.head_bucket(Bucket=private_bucket)
    if not _allows_public_read(s3.get_bucket_acl(Bucket=public_bucket)):
        raise RuntimeError(
            f"Document public bucket must allow public read: {public_bucket}"
        )
    if _allows_public_read(s3.get_bucket_acl(Bucket=private_bucket)):
        raise RuntimeError(
            f"Document private bucket must not allow public read: {private_bucket}"
        )


def execute(context: RunContext):
    if context.options.workers != 1 or context.options.limit is not None:
        raise ValueError('Transfer requires one worker and no candidate limit')
    if context.should_stop():
        return {'kind': 'maintenance.document_s3_upload_summary', 'outcome': 'stopped', 'stopped': True}
    settings = load_document_storage_settings(load_runtime_config())
    with ExitStack() as resources:
        repository = PostgresDocumentSyncRepository(context.db.database_url, schema=context.db.schema)
        resources.callback(repository.dispose)
        repository.preflight()
        if not repository.count_pending_documents():
            return run_document_upload(repository=repository, yadisk=None, primary_s3=None,
                                       settings=settings, context=context)
        yadisk = YaDisk(settings.yadisk_token)
        resources.callback(yadisk.close)
        yadisk.default_args.update(timeout=(10, 30), poll_timeout=60, n_retries=2)
        primary_s3 = _create_s3_client(settings.primary)
        resources.callback(primary_s3.close)
        _validate_primary_buckets(primary_s3, settings.public_bucket, settings.private_bucket)
        return run_document_upload(repository=repository, yadisk=yadisk, primary_s3=primary_s3,
                                   settings=settings, context=context)
