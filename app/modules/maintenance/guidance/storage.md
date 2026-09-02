# Maintenance document storage

- Backblaze B2 via S3 is primary document/derived storage under `documents.primary_storage`; `yandex.cloud` is legacy upstream storage.
- `maintenance.monocorpus_sync` owns Yandex traversal, catalog discovery, unrestricted publishing, and guarded cleanup. It never downloads document bytes or uploads to Backblaze.
- `maintenance.sync_documents_s3` queues only existing PostgreSQL rows with null/blank `document_url`. It never traverses Yandex, discovers documents, publishes links, or inserts rows.
- Upload source order is an MD5-valid shared cache entry, then direct download from persisted `ya_path`. Unavailable downloads are reported skips; setup and identity errors are fatal.
- Enforce `documents.cache_max_gib` through the shared cache helper. Cache hits refresh file recency, and eviction must tolerate paths disappearing from an already-built cache index.
- An existing object may be checkpointed after size plus submitted `source-md5`, or plain MD5 ETag, matches. Confirm new uploads with `HEAD`; do not download them again for verification.
- Abort unfinished multipart uploads for the exact content-addressed key before retrying. Use callbacks and `task.progress`; graceful stop finishes the current document.
- Update URL, size, ETag, and verification timestamp only on the unchanged pending row after confirmation and restricted-object cleanup. Never insert from the upload task.
- Legacy PDF content migration is PostgreSQL-driven and strictly single-worker. It preserves flat archive/image keys, disables boto3 transfer threads, rewrites only referenced `ttimg` URLs, checkpoints cutover before source cleanup, and re-verifies Backblaze before deleting Yandex objects.
- Reject null/duplicate MD5 identities before remote work. Do not alter the database constraint without owner approval.
- During catalog traversal, persist and execute a resource-scoped `corrupted` move for every zero-byte file before publication or catalog insertion. Resource scope keeps distinct empty paths independent despite their shared MD5.
