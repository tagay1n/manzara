# Maintenance flow guidance

These rules apply to `app/modules/maintenance/`.

Yandex Sync and its cleanup executor use the normalized catalog and shared task runtime through `scripts/run_daily_maintenance.py`; they are absent from the interactive CLI task list. The scheduled Google Sheets export uses `app/catalog/export.py` for a read-only normalized snapshot; scheduled operations and configuration live in `docs/operations.md`. Backblaze document transfer uses catalog-native checkpoints through its separate GitHub workflow; it is absent from the CLI and writes logs only to stdout. Live transfer verification remains pending. Static inspection does not establish remote execution readiness; legacy SQL names do not establish current compatibility. See `docs/catalog-model.md` from the repo root.

The retained `maintenance.monocorpus_meta_evaluate` task ID is implemented by Library and uses its publication-based [metadata contract](../library/guidance/metadata.md), including local SQLite AI retry checkpoints.

Read only the guidance matching the changed behavior:

| Area | Guidance |
| --- | --- |
| catalog traversal, Backblaze storage, cache | `guidance/storage.md` |
| guarded Yandex/S3 cleanup and locking | `guidance/cleanup.md` |

Maintenance storage/cleanup tasks must be resumable, idempotent at their safe boundaries, explicit about item/setup failures, and backed by PostgreSQL checkpoints.
