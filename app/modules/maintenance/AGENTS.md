# Maintenance flow guidance

These rules apply to `app/modules/maintenance/`.

## Document synchronization

- Backblaze B2 via S3 is the primary document store and is configured only under `documents.primary_storage`. `yandex.cloud` remains legacy document/upstream/preview storage.
- Yandex Disk is ingest/provenance only. Sync never publishes, deletes, trashes, or moves its documents.
- `maintenance.sync_documents_s3` discovers from the configured Yandex root. A persisted Backblaze `document_url` checkpoint skips every per-object Backblaze request.
- Without that checkpoint, acquire bytes in order: hash-valid local cache, verified Backblaze object, verified legacy Yandex S3 object, then Yandex Disk.
- Discovery and transfer are one sequential streaming pipeline. Process each first-seen MD5 immediately; do not wait for complete source or bucket inventories.
- Confirm new uploads with `HEAD`; size and submitted `source-md5` must match before the PostgreSQL checkpoint. Do not download a new upload just to verify it.
- Use boto3 callbacks and `task.progress`. Graceful stop completes the current document and exits at the next document boundary.
- Abort unfinished multipart uploads for the exact content-addressed key before retrying. Never treat an incomplete multipart upload as resumable.
- The shared document cache is verified input and persistent output for source downloads; cache-only files are not discovered documents.
- Restricted documents use the private bucket and backend-generated short-lived signed URLs.
- Persist `document_url` only after upload confirmation and restricted-object cleanup. Later runs trust it without remote probes. A content-addressed re-upload after a failed DB commit is acceptable.
- Treat MD5 as application identity because `public.document` has no uniqueness constraint. Reject null/duplicate identities before remote work; use transactional update-then-insert and roll back if an update matches multiple rows. Do not alter the constraint without owner approval.
- Completed sync emits reconciliation: source files, canonical documents, DB rows before/after, synced/unsynced, DB-only, duplicate paths, item failures, and `fully_synced`.
- Exact reconciliation requires completed Yandex traversal. A stopped traversal reports `discovery_complete=false`, does not evaluate DB-only rows, and never claims `fully_synced`.
- Reconciliation differences are successful reportable outcomes. Fail only when discovery or another task error prevents a trustworthy report.

## Cleanup and locking

- Any document Yandex move/removal requires a prior PostgreSQL `document_cleanup_queue` row and the guarded cleanup executor.
- `maintenance.monocorpus_sync` applies persisted cleanup and synchronizes the catalog. Duplicate-MD5 resources may be queued and executed during traversal. It may publish ordinary unrestricted documents missing public URLs; it never publishes restricted documents.
- A cleanup move completes only after the target is MD5-verified, managed S3 derivatives are removed, and dependent PostgreSQL state is deleted. Every phase is resumable and idempotent.
- `maintenance.monocorpus_sync` and `maintenance.sync_documents_s3` share one PostgreSQL advisory lock and never overlap.
