"""Internal GitHub workflow runner for catalog-native Backblaze transfer."""


import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runtime_config import config_integer
from app.task_runtime.logging import log_message
from app.task_runtime.reporting import report_run


def _descriptor():
    from app.cli.task_registry import build_descriptors
    from app.modules.maintenance.tasks import MAINTENANCE_DOCUMENT_S3_SYNC_TASK_ID

    return build_descriptors((MAINTENANCE_DOCUMENT_S3_SYNC_TASK_ID,))[0]


def _preflight(db):
    from app.document_storage import load_document_storage_settings
    from app.modules.maintenance.document_sync_repository import (
        PostgresDocumentSyncRepository,
    )
    from app.runtime_config import load_runtime_config

    load_document_storage_settings(load_runtime_config())
    repository = PostgresDocumentSyncRepository(db.database_url, schema=db.schema)
    try:
        repository.preflight()
    finally:
        repository.dispose()


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
            budget_seconds=config_integer("maintenance", "transfer_budget_seconds"),
                              on_result=lambda run: report_run(run, results))
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
