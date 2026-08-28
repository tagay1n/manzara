# Maintenance flow guidance

These rules apply to `app/modules/maintenance/`.

Read only the guidance matching the changed behavior:

| Area | Guidance |
| --- | --- |
| catalog traversal, Backblaze upload, cache | `guidance/storage.md` |
| guarded Yandex/S3 cleanup and locking | `guidance/cleanup.md` |

Maintenance tasks must be resumable, idempotent at their safe boundaries, explicit about item/setup failures, and backed by PostgreSQL checkpoints.
