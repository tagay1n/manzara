# Maintenance cleanup and locking

- Every Yandex move/removal requires a prior PostgreSQL `document_cleanup_queue` row and the guarded executor.
- `maintenance.monocorpus_sync` applies persisted cleanup and synchronizes the catalog. Duplicate-MD5 resources may be queued during traversal; unrestricted missing links may be published, but restricted links may not.
- A cleanup move completes only after target MD5 verification, managed S3 derivative removal, and dependent PostgreSQL cleanup. Every phase is resumable and idempotent.
- Catalog sync and Backblaze upload may run concurrently. Upload checkpoints must revalidate the pending row's source identity and remove objects created by stale attempts.
