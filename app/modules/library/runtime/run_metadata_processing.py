"""Explicit CLI orchestration for publication metadata extraction/evaluation."""

import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.artifacts import workspace_dir
from app.catalog.contracts import CatalogConflict
from app.catalog.metadata_processing import MetadataProcessingStore
from app.document_storage import load_document_storage_settings, prune_document_cache
from app.gemini_config import load_required_gemini_model_pool
from app.gemini_model_pool import (
    GeminiModelPoolExhaustedError,
    GeminiModelPoolItemRejectedError,
    GeminiModelPoolOperationalError,
    GeminiModelPoolUnavailableError,
    run_ordered_model_pool,
)
from app.gemini_requests import generate_structured_json
from app.gemini_runtime import GeminiRuntimeManager, GeminiStopRequestedError
from app.local_state import AIItemCheckpointStore
from app.modules.library.corrupt_document import (
    CorruptDocumentError,
    build_corrupt_cleanup_plan,
)
from app.modules.library.metadata_contract import (
    CONTRACT_VERSION,
    metadata_contract_issues,
)
from app.modules.library.metadata_extraction import (
    PROMPT_VERSION,
    ExtractedMetadata,
    MetadataExtractionCandidate,
    metadata_quality_issue,
    parse_metadata_response,
    prepare_metadata_request,
)
from app.modules.library.runtime.metadata.evaluation_evidence import (
    prepare_evaluation_request,
)
from app.modules.library.runtime.metadata.evaluation_response import (
    _parse_evaluation_response,
    _schema_after_evaluation,
)
from app.modules.library.runtime.metadata.evaluation_types import (
    Evaluation,
    EvaluationTask,
)
from app.operational_state import OperationalStateStore
from app.postgres_engine import get_postgres_engine, is_transient_postgres_error
from app.repositories.document_cleanup import DocumentCleanupRepository
from app.runtime_config import (
    config_integer,
    load_runtime_config,
    required_integer,
)
from app.s3_transfer import create_s3_client
from app.task_runtime.logging import redact

EVALUATION_PROMPT_VERSION = 'prompt.v4'
FLOWS = {'extract': 'library.metadata_extract.catalog.v1', 'evaluate': 'library.metadata_evaluate.catalog.v1'}


def _cooldown(config):
    return required_integer(config, 'gemini', 'metadata_extraction_operational_retry_cooldown_seconds',
                            minimum=60, maximum=604_800)


def _needs_extraction(schema):
    return (bool(metadata_contract_issues(schema)) or metadata_quality_issue(schema) is not None
            or not str(schema.get('inLanguage') or '').strip())


def _can_retry(state, *, version, models):
    if not state or state['contract_version'] != version:
        return True
    if state['status'] == 'terminal' and set(state.get('model_pool') or []) == set(models):
        return False
    if state.get('retry_after'):
        return datetime.fromisoformat(state['retry_after'].replace('Z', '+00:00')) <= datetime.now(timezone.utc)
    return True


def _candidate(source):
    path = str(source.get('source_path') or '').replace('\\', '/')
    return MetadataExtractionCandidate(md5=source['md5'], mime_type=str(source['mime_type'] or '').lower().strip(),
        document_url=source['document_url'], content_url=source['content_url'],
        upstream_metadata=source['upstream_metadata'], primary_storage_size=source['primary_storage_size'],
        source_filename=path.rsplit('/', 1)[-1], source_path=path)


class Progress:
    def __init__(self, context, total):
        self.context = context
        self.total = total
        self.counters = Counter(succeeded=0, review_required=0, failed=0, terminal=0, source_deferred=0,
                                quota_deferred=0, service_deferred=0, checkpoint_raced=0,
                                corrupted_planned=0, corrupted_plan_reused=0)
        self.current = 0
        self.attempts = Counter()
        self.successes = Counter()

    def publish(self, *, force=False):
        self.context.progress({'phase': 'processing', 'current': self.current, 'total': self.total,
            'percent': round(100 * self.current / self.total, 2) if self.total else 100,
            'remaining': max(0, self.total - self.counters['succeeded'] - self.counters['review_required'] - self.counters['terminal']),
            **self.counters, 'model_attempts': dict(self.attempts), 'model_successes': dict(self.successes)}, force=force)

    def attempt(self, model):
        self.attempts[model] += 1
        self.publish()

    def complete(self, outcome, model=None):
        self.current += 1
        self.counters[outcome] += 1
        if model:
            self.successes[model] += 1
        self.publish()


class Processor:
    def __init__(self, context, mode, store, config, storage, models, version, checkpoints, progress, cooldown, known):
        self.context, self.mode, self.store = context, mode, store
        self.config, self.storage, self.models = config, storage, models
        self.version, self.checkpoints, self.progress = version, checkpoints, progress
        self.flow, self.cooldown, self.known = FLOWS[mode], cooldown, known
        self.workspace = workspace_dir('library', 'metadata-' + mode, run_id=context.run_id)
        self.abort = False
        self.global_unavailable = False
        self.results = []
        self.manager = GeminiRuntimeManager(context.db, task_id=context.task_id, should_stop=self.stopped)

    def stopped(self):
        return self.context.should_stop() or self.abort or self.global_unavailable

    def log(self, text):
        self.context.log(text)

    def _checkpoint_args(self, source):
        return dict(flow_id=self.flow, item_id=source['checkpoint_id'], contract_version=self.version,
                    models=self.models, run_id=self.context.run_id)

    def defer(self, source, error, retry_at=None):
        retry_at = retry_at or datetime.now(timezone.utc) + timedelta(seconds=self.cooldown)
        self.checkpoints.record_deferral(**self._checkpoint_args(source),
            retry_after=retry_at.isoformat(), error=redact(error))

    def finish(self, publication, outcome, *, source=None, model=None, proposals=None, error=None):
        record = {'publication_id': publication['publication_id'], 'outcome': outcome}
        if source:
            record['md5'] = source['md5']
        if model:
            record['model'] = model
        if proposals:
            record['proposal_ids'] = proposals
        if error:
            record['error'] = redact(error)
        self.results.append(record)
        self.progress.complete(outcome, model)
        self.log('Metadata item final ' + json.dumps(record, ensure_ascii=False))

    def _prepare(self, candidate, source, primary_s3):
        if self.mode == 'extract':
            request = prepare_metadata_request(candidate, workspace=self.workspace,
                storage=self.storage, primary_s3=primary_s3)
            return list(request.contents), request.files, ExtractedMetadata, parse_metadata_response
        doc = EvaluationTask(md5=source['md5'], publication_id=source['publication_id'], schema_org=source['schema_org'])
        prompt, files = prepare_evaluation_request(candidate, source['schema_org'], workspace=self.workspace,
            storage=self.storage, primary_s3=primary_s3, known=self.known, log=self.log)
        return prompt, files, Evaluation, lambda raw: _parse_evaluation_response(raw, doc=doc, config=self.config)

    def _plan_corruption(self, publication, source, candidate, error):
        if self.mode != 'extract':
            return
        if not candidate.source_path:
            self.log(f'Corrupt source md5={candidate.md5} has no source path; guarded cleanup planning is deferred')
            return
        plan = build_corrupt_cleanup_plan(storage=self.storage, md5=candidate.md5,
            source_path=candidate.source_path, mime_type=candidate.mime_type,
            source_size=candidate.primary_storage_size, task_id=self.context.task_id,
            run_id=self.context.run_id, error=error)
        repository = DocumentCleanupRepository(self.context.db.database_url, schema=self.context.db.schema)
        try:
            with self.store.mutation(publication, source) as (conn, _):
                cleanup_id, created = repository.enqueue_cleanup(plan, conn=conn)
            self.progress.counters['corrupted_planned' if created else 'corrupted_plan_reused'] += 1
            payload = {'kind': 'library.metadata_corruption_plan', 'publication_id': publication['publication_id'],
                       'md5': candidate.md5, 'cleanup_id': cleanup_id, 'created': created}
            path = self.workspace / f'corruption-{candidate.md5}.json'
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            self.context.artifact({**payload, 'plan_path': str(path)})
            self.log(f'Corruption plan md5={candidate.md5} cleanup_id={cleanup_id} created={created}')
        finally:
            repository.dispose()

    def process(self, publication):
        if self.stopped():
            return
        if not publication['sources']:
            self.finish(publication, 'source_deferred', error='No complete, unrestricted evidence file with verified primary storage')
            return
        primary_s3 = None
        try:
            primary_s3 = create_s3_client(self.storage.primary, profile="metadata")
            self._process_sources(publication, self.manager, primary_s3)
        except BaseException as exc:
            self.abort = True
            recorded = any(row['publication_id'] == publication['publication_id'] for row in self.results)
            if not recorded:
                self.finish(publication, 'failed', error=exc)
            raise
        finally:
            if primary_s3 is not None:
                primary_s3.close()

    def _process_sources(self, publication, manager, primary_s3):
        last_error = None
        terminal = 0
        for source in publication['sources']:
            if self.stopped():
                return
            state = self.checkpoints.get(self.flow, source['checkpoint_id'])
            if not _can_retry(state, version=self.version, models=self.models):
                terminal += int(bool(state and state['status'] == 'terminal'))
                continue
            candidate = _candidate(source)
            self.log(f'Metadata item start publication_id={publication["publication_id"]} md5={source["md5"]}')
            try:
                prompt, files, response_schema, parse = self._prepare(candidate, source, primary_s3)
            except Exception as exc:
                if isinstance(exc, CorruptDocumentError):
                    try:
                        self._plan_corruption(publication, source, candidate, exc)
                    except CatalogConflict as conflict:
                        self.finish(publication, 'checkpoint_raced', source=source, error=conflict)
                        return
                if isinstance(exc, CatalogConflict):
                    self.finish(publication, 'checkpoint_raced', source=source, error=exc)
                    return
                last_error = exc
                self.defer(source, exc)
                self.log(f'Source preparation deferred md5={source["md5"]}: {exc}; trying another eligible file')
                continue
            if self.stopped():
                return

            def request(model, api_key, _lease):
                self.log(f'Gemini request publication_id={publication["publication_id"]} md5={source["md5"]} model={model}')
                self.progress.attempt(model)
                return generate_structured_json(api_key=api_key, model_name=model, contents=prompt,
                    response_schema=response_schema, files=files, timeout_seconds=config_integer("gemini", "request", "metadata_timeout_seconds"))

            def failure(model_name, kind, error):
                self.checkpoints.record_failure(**self._checkpoint_args(source), model_name=model_name,
                    kind=kind, error=redact(error))
                self.log(f'Model failure md5={source["md5"]} model={model_name} kind={kind} error={error}')

            attempts = (state.get('attempts') or []) if state and state['contract_version'] == self.version else []
            try:
                result = run_ordered_model_pool(manager=manager, models=self.models, request=request, parse=parse,
                    record_failure=failure, run_id=self.context.run_id,
                    already_attempted={row['model'] for row in attempts})
                if self.stopped():
                    return
                evaluation = None
                schema = result.value
                if self.mode == 'evaluate':
                    schema = _schema_after_evaluation(source['schema_org'], result.value)
                    evaluation = {'applicable': result.value.applicable, 'reason': result.value.reason,
                                  'ddc': result.value.library_ddc, 'path': result.value.library_path}
                proposals = self.store.save(publication, source, schema, actor=self.context.task_id,
                    method=f'{result.model_name}/{self.version}', evaluation=evaluation)
                for prior in publication['sources']:
                    self.checkpoints.clear(self.flow, prior['checkpoint_id'])
                self.finish(publication, 'review_required' if proposals else 'succeeded', source=source,
                            model=result.model_name, proposals=proposals)
                return
            except (GeminiModelPoolExhaustedError, GeminiModelPoolItemRejectedError) as exc:
                self.checkpoints.mark_terminal(**self._checkpoint_args(source), reason=redact(exc))
                self.finish(publication, 'terminal', source=source, error=exc)
                return
            except GeminiModelPoolUnavailableError as exc:
                self.defer(source, exc, exc.retry_at)
                if exc.all_models_unavailable:
                    self.global_unavailable = True
                self.finish(publication, 'quota_deferred', source=source, error=exc)
                return
            except GeminiModelPoolOperationalError as exc:
                if not exc.retryable:
                    self.finish(publication, 'failed', source=source, error=exc)
                    self.abort = True
                    raise
                self.defer(source, exc, exc.retry_at)
                self.finish(publication, 'service_deferred', source=source, error=exc)
                return
            except GeminiStopRequestedError:
                return
            except CatalogConflict as exc:
                self.finish(publication, 'checkpoint_raced', source=source, error=exc)
                return
            except Exception as exc:
                if not is_transient_postgres_error(exc):
                    self.finish(publication, 'failed', source=source, error=exc)
                    self.abort = True
                    raise
                self.defer(source, exc)
                self.finish(publication, 'service_deferred', source=source, error=exc)
                return
        self.finish(publication, 'terminal' if terminal == len(publication['sources']) and terminal else 'source_deferred', error=last_error)


def execute(context, *, mode):
    if context.options.per_mime_limit is not None or context.options.only_md5s or context.options.retry_known_failures:
        raise ValueError('Metadata tasks support a publication limit; clear MIME/source/retry options')
    summary = {'kind': 'library.metadata_extraction_summary' if mode == 'extract' else 'library.metadata_evaluation_summary',
               'unit': 'publication', 'checkpoint_namespace': FLOWS[mode]}
    if context.should_stop():
        return {**summary, 'outcome': 'stopped', 'stopped': True}
    store = MetadataProcessingStore(get_postgres_engine(context.db.database_url, schema=context.db.schema), schema=context.db.schema)
    store.preflight()
    config = load_runtime_config()
    cooldown = _cooldown(config)
    models = load_required_gemini_model_pool()
    version = PROMPT_VERSION if mode == 'extract' else EVALUATION_PROMPT_VERSION
    storage = load_document_storage_settings(config)
    checkpoints = AIItemCheckpointStore(context.db.local_state_path)
    quality = OperationalStateStore(context.db.local_state_path)
    context.log('Metadata processing: reading publication inventory')
    try:
        inventory = store.inventory(context.should_stop)
    except InterruptedError:
        return {**summary, 'outcome': 'stopped', 'stopped': True}
    languages = set(config['sup_langs']['tt']['codes']) if mode == 'evaluate' else set()
    candidates = []
    skipped = Counter()
    for publication in inventory:
        if context.should_stop():
            return {**summary, 'outcome': 'stopped', 'stopped': True}
        if publication.get('awaiting_review'):
            skipped['awaiting_review'] += 1
            continue
        if mode == 'evaluate':
            if not languages.intersection(publication.get('languages', [])):
                continue
            if publication['inclusion'] != 'pending' and ((publication['inclusion'] == 'included') == (publication['classification_id'] is not None)):
                skipped['already_complete'] += 1
                continue
        schema = publication.get('schema_org', {})
        invalid = not publication['metadata_present'] or _needs_extraction(schema)
        if schema:
            fingerprint = hashlib.sha256(json.dumps({'schema_org': schema, 'metadata_present': publication['metadata_present']},
                                                     sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            cache = quality.get('library.metadata_quality.catalog.v1', publication['publication_id'])
            if not cache or cache.get('input_hash') != fingerprint or cache.get('contract_version') != CONTRACT_VERSION:
                quality.put('library.metadata_quality.catalog.v1', publication['publication_id'], {
                'contract_version': CONTRACT_VERSION, 'input_hash': fingerprint,
                'status': 'invalid' if invalid else 'resolved',
                'issues': metadata_contract_issues(schema), 'quality_issue': metadata_quality_issue(schema)})
        if mode == 'extract' and not invalid:
            skipped['already_complete'] += 1
            continue
        if mode == 'evaluate' and invalid:
            skipped['needs_extraction'] += 1
            continue
        if publication['sources']:
            states = checkpoints.get_many(FLOWS[mode], [source['checkpoint_id'] for source in publication['sources']])
            if not any(_can_retry(states.get(source['checkpoint_id']), version=version, models=models) for source in publication['sources']):
                skipped['retry_excluded'] += 1
                continue
        candidates.append(publication)
        if context.options.limit is not None and len(candidates) >= context.options.limit:
            break
    progress = Progress(context, len(candidates))
    progress.publish()
    known = store.known_classifications() if mode == 'evaluate' and candidates else []
    prune_document_cache(storage.cache_path, max_bytes=storage.cache_max_bytes)
    processor = Processor(context, mode, store, config, storage, models, version, checkpoints, progress, cooldown, known)
    context.log(f'Metadata processing: mode={mode} publications={len(candidates)}')
    fatal = None
    for publication in candidates:
        if processor.stopped():
            break
        try:
            processor.process(publication)
        except Exception as exc:
            fatal = exc
            break
    progress.publish(force=True)
    items_path = processor.workspace / 'items.json'
    items_path.write_text(json.dumps(sorted(processor.results, key=lambda row: row['publication_id']),
                                    ensure_ascii=False, indent=2), encoding='utf-8')
    context.artifact({'kind': 'library.metadata_item_results', 'items_path': str(items_path), 'processed': progress.current})
    counters = dict(progress.counters)
    deferred = (any(counters.get(key, 0) for key in ('source_deferred', 'quota_deferred', 'service_deferred', 'review_required', 'terminal'))
                or bool(skipped['awaiting_review']) or bool(skipped['retry_excluded']) or bool(skipped['needs_extraction']))
    outcome = 'failed' if fatal or counters.get('checkpoint_raced') else (
        'stopped' if context.should_stop() else 'deferred' if deferred or processor.global_unavailable else 'completed')
    error = redact(fatal) if fatal else ('Metadata snapshots changed; inspect items and resume with a fresh inventory'
                                       if counters.get('checkpoint_raced') else None)
    return {**summary, 'outcome': outcome, 'stopped': context.should_stop(), 'processed': progress.current,
            'total': len(candidates), 'eligible': len(candidates),
            'remaining': max(0, len(candidates) - counters['succeeded'] - counters['review_required'] - counters['terminal']),
            'already_complete': skipped['already_complete'], 'unprocessed': len(candidates) - progress.current, **counters,
            'skipped': dict(skipped), 'model_attempts': dict(progress.attempts), 'model_successes': dict(progress.successes),
            'items_path': str(items_path), 'workspace_path': str(processor.workspace),
            **({'error': error} if error else {})}
