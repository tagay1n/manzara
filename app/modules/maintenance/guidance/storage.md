# Maintenance storage

Backblaze B2/S3 is primary document/derived storage under `documents.primary_storage`. [Operations](../../../../docs/operations.md) owns runner/configuration; [cleanup](cleanup.md) owns execution/locks.

## Yandex catalog sync

`maintenance.monocorpus_sync` traverses Yandex, discovers catalog files, publishes unrestricted links, and executes guarded cleanup. It never downloads/uploads document bytes.

- New MD5 creates publication/document/source atomically; preserve existing selection/bibliography/protected fields and never widen privacy. Unchanged facts create no audit. Restricted documents persist null public URL/key even when remote/catalog links exist; Sync does not revoke Yandex publication.
- Match MD5s against one normalized PostgreSQL snapshot; buffer ordinary commands until traversal completes. Virtual choices deduplicate new MD5s without changing membership. Stop/abort discards buffers, then reruns traverse again; cleanup is the persisted exception.
- Invalid resource paths count failures with escaped diagnostics while valid resources proceed; invalid roots/outside-root resources are fatal. Skipped directories skip subtrees; correction is required for success.
- Apply at most 250 unique MD5s per transaction with set-based writes/audits. Lock/recheck revisions, protection, verified MIME, privacy, cleanup. Conflicted files stay unchanged; other valid files may commit. Stop between batches; no transaction spans traversal/provider calls.
- Publish missing unrestricted links only after catalog acceptance and immediate catalog/privacy/remote-identity recheck. Bulk-checkpoint responses; safe stop completes the request and flushes returned links, with traversal recovering prior remote publication. Report scanned/buffered/committed/pending commands/publications.
- Restore missing/invalid/replaced canonical paths only from expected-MD5 resources; never probe empty paths. Duplicate cleanup needs a surviving same-MD5 canonical resource, including buffered new sources.
- Persist/execute resource-scoped `corrupted` plans for zero-byte paths before insertion/publication; separate empty resources despite shared MD5.
- `.pas` / `text/pascal` follows ordinary non-document filtering. Existing rows remain for Library planning; no plans for uncataloged Pascal sources.

## Backblaze transfer

`maintenance.sync_documents_s3` runs only in the separate automatic/manual GitHub workflow. Page existing normalized documents in deterministic MD5 batches of 250. Pending means blank/missing primary locator, size, nonblank ETag, or verification time; complete checkpoints stay untouched. Verify/repair incomplete valid destinations without inserting documents/publications, filtering inclusion/selection, or redirecting invalid/privacy-inconsistent locators.

- Exclude document cleanup and plans owning canonical source paths. Unknown privacy fails closed; restricted files need private bucket/encrypted locators. Respect protected checkpoint fields. Snapshot document/source/primary identity/revisions and commit guarded revisions/audits briefly.
- HEAD before acquiring bytes. Expected size plus `source-md5` or plain MD5 ETag permits reuse; multipart ETag alone does not. Insufficient evidence needs a verified source. Confirm upload by HEAD, never verification download.
- Reuse MD5-valid cache bytes without deleting them; otherwise download persisted Yandex sources into private owned temporaries. Recheck remote identity/MD5/size and disk capacity plus 1 GiB reserve. Remove owned complete/partial files at every outcome; never populate/globally prune shared cache.
- Abort multipart uploads only at the exact target key. Preserve flat `{md5}{extension}` and valid managed locators. Restricted cleanup needs current catalog privacy/identity plus verified public-object identity; confirm removal before commit.
- Shared operation locks/safe boundaries coordinate conflicts. Failures/unavailable sources/busy documents remain unresolved while independent items continue; incomplete work returns nonzero, empty backlog succeeds. Stop finishes current document. Deterministic objects recover uploads whose commits failed.
