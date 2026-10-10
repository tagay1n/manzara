"""Internal GitHub workflow runner for catalog-native Backblaze transfer."""

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.task_runtime.logging import log_message


def _descriptor():
    from app.modules.maintenance.runtime.sync_documents_s3 import TASK_ID, execute
    from app.modules.maintenance.tasks import maintenance_task_definitions
    from app.task_runtime.contracts import TaskDescriptor

    definition = next(item for item in maintenance_task_definitions() if item['task_id'] == TASK_ID)
    return TaskDescriptor(task_id=TASK_ID, title=definition['title'],
                          group='Maintenance', group_id=definition['group_id'], execute=execute)


def _preflight(db):
    from app.document_storage import load_document_storage_settings
    from app.modules.maintenance.document_sync_repository import PostgresDocumentSyncRepository
    from app.runtime_config import load_runtime_config

    load_document_storage_settings(load_runtime_config())
    repository = PostgresDocumentSyncRepository(db.database_url, schema=db.schema)
    try:
        repository.preflight()
    finally:
        repository.dispose()


def _report(run, results):
    from app.task_runtime.logging import redact

    summary = run.get('summary') or {}
    result = {'task_id': run['task_id'], 'run_id': run['run_id'], 'status': run['status'],
              'exit_code': run.get('exit_code'),
              'counters': {key: value for key, value in summary.items() if type(value) is int}}
    if run.get('error_text') or summary.get('error'):
        result['error'] = redact(run.get('error_text') or summary['error'])
    results.append(result)
    print(json.dumps(result, ensure_ascii=True), flush=True)


def _summary(results, exit_code):
    target = os.environ.get('GITHUB_STEP_SUMMARY')
    if not target:
        return
    with Path(target).open('a', encoding='utf-8') as handle:
        handle.write(f'## Backblaze document transfer\n\nCommand exit code: {exit_code}\n\n')
        for result in results:
            handle.write(f"Run {result['run_id']}: **{result['status']}**\n\n")
            handle.write('| Counter | Value |\n| --- | --- |\n')
            for key, value in result['counters'].items():
                handle.write(f'| {key} | {value} |\n')
        handle.write('\nDetailed logs are in Actions stdout; structured results are retained as diagnostics.\n')


def main():
    results = []
    exit_code = 1
    try:
        # This is an internal runner, not an additional local operations interface.
        if os.environ.get('GITHUB_ACTIONS') != 'true' or sys.argv[1:]:
            raise ValueError('Start Backblaze transfer through its GitHub workflow')
        import yaml
        from app.settings import load_settings
        from app.task_runtime.batch import run_batch

        exit_code = run_batch(load_settings(), [_descriptor()], preflight=_preflight,
                              on_result=lambda run: _report(run, results))
    except Exception as exc:
        if "yaml" in locals() and isinstance(exc, yaml.YAMLError):
            log_message("Backblaze transfer failed: runtime configuration must be valid YAML", level="ERROR")
        else:
            log_message(f"Backblaze transfer failed: {exc}", level="ERROR")
    finally:
        _summary(results, exit_code)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
