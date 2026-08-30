# Maintenance cleanup and locking

- Every Yandex move/removal requires a prior PostgreSQL `document_cleanup_queue` row and the guarded executor.
- `maintenance.monocorpus_sync` applies persisted cleanup and synchronizes the catalog. Duplicate-MD5 resources may be queued during traversal; unrestricted missing links may be published, but restricted links may not.
- A cleanup move completes only after target MD5 verification, managed S3 derivative removal, and dependent PostgreSQL cleanup. Every phase is resumable and idempotent.
- Document-owned PostgreSQL rows use foreign keys with `ON DELETE CASCADE`; cleanup deletes the `document` row and lets the database remove dependent metadata and workflow state atomically. `document_cleanup_queue` is audit/control state and intentionally survives document deletion.
- Catalog sync and Backblaze upload may run concurrently. Upload checkpoints must revalidate the pending row's source identity and remove objects created by stale attempts.
- New move targets preserve the complete path relative to the configured document source root under `filtered_out/<reason>/`. Persisted targets are immutable and remain resumable even when target policy changes later.
