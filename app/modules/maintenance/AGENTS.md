# Maintenance flow guidance

These rules apply to `app/modules/maintenance/`.

## Document catalog and storage

- Backblaze B2 via S3 is the primary document store and is configured only under `documents.primary_storage`. `yandex.cloud` remains legacy document/upstream/preview storage.
- `maintenance.monocorpus_sync` owns Yandex traversal, catalog discovery, ordinary unrestricted publishing, and guarded cleanup execution. It never downloads document bytes or uploads them to Backblaze.
- `maintenance.sync_documents_s3` uses PostgreSQL rows with null/blank `document_url` as its only queue. It never lists Yandex directories, discovers documents, publishes links, or inserts document rows.
- Upload source order is a hash-valid shared cache entry, then a direct download from the row's persisted `ya_path`. Do not download document bytes from Backblaze or legacy S3.
- An unavailable Yandex download is a reported skip that leaves the row pending. Other item failures are reported and processing continues; setup and identity errors are task-fatal.
- A matching Backblaze object may be verified with size plus submitted `source-md5` metadata or a plain MD5 ETag and checkpointed without re-uploading.
- Confirm new uploads with `HEAD`; size and submitted `source-md5` must match before the PostgreSQL checkpoint. Do not download a new upload just to verify it.
- Use boto3 callbacks and `task.progress`. Graceful stop completes the current document and exits at the next document boundary.
- Abort unfinished multipart uploads for the exact content-addressed key before retrying. Never treat an incomplete multipart upload as resumable.
- The shared document cache is verified input and persistent output for Yandex downloads; cache files never create queue entries.
- Restricted documents use the private bucket and backend-generated short-lived signed URLs.
- Persist `document_url`, size, ETag, and verification timestamp only after upload confirmation and restricted-object cleanup. Update only a still-pending existing row; never insert from the upload task.
- Treat MD5 as application identity because `public.document` has no uniqueness constraint. Reject null/duplicate identities before remote work. Do not alter the constraint without owner approval.
- Completed uploads emit queue reconciliation: pending before/after, processed, uploaded, recovered, cache/Yandex sources, skipped downloads, item failures, and stop state.

## Cleanup and locking

- Any document Yandex move/removal requires a prior PostgreSQL `document_cleanup_queue` row and the guarded cleanup executor.
- `maintenance.monocorpus_sync` applies persisted cleanup and synchronizes the catalog. Duplicate-MD5 resources may be queued and executed during traversal. It may publish ordinary unrestricted documents missing public URLs; it never publishes restricted documents.
- A cleanup move completes only after the target is MD5-verified, managed S3 derivatives are removed, and dependent PostgreSQL state is deleted. Every phase is resumable and idempotent.
- `maintenance.monocorpus_sync` and `maintenance.sync_documents_s3` may run concurrently. Upload checkpoints must validate that the pending row's source identity is unchanged and remove objects created by stale attempts.
