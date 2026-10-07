# Maintenance flow guidance

These rules apply to `app/modules/maintenance/`.

Catalog-dependent sync, cleanup, and export code needs adaptation/verification against the migrated PostgreSQL model. Guidance preserves required safety behavior; legacy SQL names do not establish current compatibility. See `docs/catalog-model.md` from the repo root.

Read only the guidance matching the changed behavior:

| Area | Guidance |
| --- | --- |
| catalog traversal, Backblaze storage, cache | `guidance/storage.md` |
| guarded Yandex/S3 cleanup and locking | `guidance/cleanup.md` |

Maintenance tasks must be resumable, idempotent at their safe boundaries, explicit about item/setup failures, and backed by PostgreSQL checkpoints.
