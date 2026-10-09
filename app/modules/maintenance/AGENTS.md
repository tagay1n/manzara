# Maintenance flow guidance

These rules apply to `app/modules/maintenance/`.

Yandex Sync and its cleanup executor use the normalized catalog and inline CLI runtime. Backblaze upload and export remain pending catalog adaptation/verification. Static inspection does not establish remote execution readiness; legacy SQL names do not establish current compatibility. See `docs/catalog-model.md` from the repo root.

Read only the guidance matching the changed behavior:

| Area | Guidance |
| --- | --- |
| catalog traversal, Backblaze storage, cache | `guidance/storage.md` |
| guarded Yandex/S3 cleanup and locking | `guidance/cleanup.md` |

Maintenance tasks must be resumable, idempotent at their safe boundaries, explicit about item/setup failures, and backed by PostgreSQL checkpoints.
