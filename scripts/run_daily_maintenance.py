"""Prepare catalog cleanup, then execute guarded cleanup and Yandex Sync."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

# Direct script execution puts scripts/, rather than the repository, on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runtime_config import config_integer
from app.task_runtime.logging import log_message
from app.task_runtime.reporting import report_run


def _descriptors():
    from app.cli.task_registry import build_descriptors
    from app.modules.library.tasks import LIBRARY_PREPARE_DOCUMENT_CLEANUP_TASK_ID
    from app.modules.maintenance.tasks import MAINTENANCE_MONOCORPUS_SYNC_TASK_ID

    return build_descriptors((LIBRARY_PREPARE_DOCUMENT_CLEANUP_TASK_ID,
                              MAINTENANCE_MONOCORPUS_SYNC_TASK_ID))


def _preflight(db) -> None:
    from app.document_cleanup_paths import cleanup_target_path, source_path
    from app.document_storage import load_document_storage_settings
    from app.modules.maintenance.monocorpus_sync_repository import (
        MonocorpusSyncRepository,
    )
    from app.runtime_config import load_runtime_config

    storage = load_document_storage_settings(load_runtime_config())
    source_path(storage.source_path)
    source_path(storage.restricted_path)
    cleanup_target_path(storage.filtered_out_path, reason="corrupted",
                        source_root_path=storage.source_path, source_path=storage.source_path.rstrip("/") + "/file")
    # Missing derivative buckets would silently leave managed objects behind.
    for name, bucket in (("book_previews", storage.preview_bucket), ("content", storage.content_bucket),
                         ("content_images", storage.content_images_bucket)):
        if not bucket:
            raise ValueError(f"Configure documents.primary_storage.bucket.{name}")
    if db.get_pool_metrics()["max_size"] < 3:
        raise ValueError("Maintenance requires database_pool_size >= 3 for sync and document operation locks")
    repository = MonocorpusSyncRepository(db.database_url, schema=db.schema)
    try:
        repository.catalog.preflight()
    finally:
        repository.dispose()


def _github_summary(results, exit_code: int) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    with Path(target).open("a", encoding="utf-8") as handle:
        handle.write(f"## Daily sync and cleanup\n\nCommand exit code: {exit_code}\n\n")
        handle.write("| Stage | Status | Counters |\n| --- | --- | --- |\n")
        for title, task_id in (("Cleanup preparation", "library.prepare_document_cleanup"),
                               ("Sync and cleanup execution", "maintenance.monocorpus_sync")):
            result = next((item for item in results if item["task_id"] == task_id), None)
            status = result["status"] if result else "not run"
            counters = ", ".join(f"{key}={value}" for key, value in result["counters"].items()) if result else ""
            handle.write(f"| {title} | {status} | {counters} |\n")
        handle.write("\nDetailed diagnostics are in Actions stdout and structured result artifacts.\n")


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    results: list[dict[str, Any]] = []
    exit_code = 1
    try:
        import yaml

        from app.settings import load_settings
        from app.task_runtime.batch import run_batch

        settings = load_settings()
        exit_code = run_batch(settings, _descriptors(), preflight=_preflight,
            budget_seconds=config_integer("maintenance", "daily_budget_seconds"),
                              on_result=lambda run: report_run(run, results))
    except Exception as exc:
        if "yaml" in locals() and isinstance(exc, yaml.YAMLError):
            log_message("Daily maintenance failed: runtime configuration must be valid YAML", level="ERROR")
        else:
            log_message(f"Daily maintenance failed: {exc}", level="ERROR")
    finally:
        _github_summary(results, exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
