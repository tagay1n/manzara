# Document cleanup review

Daily maintenance prepares `library.prepare_document_cleanup`, then runs `maintenance.monocorpus_sync`; [operations](operations.md#scheduled-sync-and-cleanup) owns launch/schedule/configuration.

Preparation reads normalized publications/documents/languages/ISBNs/Yandex locations. Plan non-document formats and known-language publications with no Tatar; retain unknown language and multilingual Tatar. Skip files without source paths. Refresh unexecuted matching automatic plans' reviewed facts while keeping immutable targets. Planning never moves/deletes files or catalog rows and needs only source/filtered-out paths, not storage credentials.

Shared document locks defer busy plans into `plans_deferred`; independent planning continues, but incomplete preparation prevents Sync. Duplicate ISBN groups always need explicit review, even with recommended complete PDFs: equality does not prove editions/files interchangeable.

Close the interactive CLI before these noninteractive JSON commands; they use its session lock:

```bash
python -m app cleanup reviews
python -m app cleanup reviews --status decided --limit 100
python -m app cleanup decide REVIEW_ID --snapshot REVIEW_SNAPSHOT --keep MD5 [MD5 ...]
python -m app cleanup undo REVIEW_ID
python -m app cleanup queue --limit 100
```

Use listed `review_snapshot` and full retained MD5s. Decisions recheck publication/file/source revisions; changed/old incomplete snapshots need preparation and fresh review. Mutation transactions explicitly request read-write even under read-only database defaults; inspection inherits defaults. Decision/plans commit together; identical decisions add nothing, conflicts fail. Undo requires exclusive plans still reversible; execution-phase failures cannot be undone as unstarted work. Decided reviews remain audit history.

PostgreSQL owns plans/approvals/phases/cancellation/completion; SQLite owns attempts/run IDs/transient errors. Safe stop finishes persisted item boundaries; reruns reuse active plans/reviews. Preparation/review alone never execute the queue. Sync executes plans before discovery and may perform resource cleanup during traversal under [Maintenance rules](../app/modules/maintenance/guidance/cleanup.md). Newly discovered files enter the next preparation cohort. Historical schema transfer: [database tools](operations.md#database-tools).
