# Maintenance cleanup and locking

- Every Yandex move/removal requires a prior PostgreSQL `document_cleanup_queue` row and the guarded executor.
- `maintenance.monocorpus_sync` applies persisted cleanup and synchronizes the catalog. Duplicate-MD5 resources may be queued during traversal; unrestricted missing links may be published, but restricted links may not.
- A cleanup move completes only after target MD5 verification, managed S3 derivative removal, and dependent PostgreSQL cleanup. Every phase is resumable and idempotent.
- Document cleanup also removes completed entries for the MD5 from the shared local source cache. Pending ISBN reviews are reconciled in the same PostgreSQL transaction as document deletion: missing candidates are removed, obsolete groups are superseded, and decided reviews remain unchanged as audit history.
- Use the catalog-owned document deletion path and verify file-owned foreign-key cascades and retained checkpoints. Publication metadata now belongs to publications: do not assume deleting a file through the legacy `document` view deletes its publication or shared bibliographic data. Explicitly remove detached `library_upstream_metadata` for the MD5 in the same transaction. `document_cleanup_queue` is audit/control state and survives document deletion.
- Catalog sync and Backblaze upload may run concurrently. Upload checkpoints must revalidate the pending row's source identity and remove objects created by stale attempts.
- New move targets preserve the complete path relative to the configured document source root under `filtered_out/<reason>/`. Persisted targets are immutable and remain resumable even when target policy changes later.
- Library cleanup preparation marks existing Pascal catalog rows as `non_document`. Newly discovered Pascal files are filtered by catalog sync like other non-document files.
