# Document cleanup

Daily maintenance runs cleanup preparation (`library.prepare_document_cleanup`) before Sync (`maintenance.monocorpus_sync`). Preparation reads normalized catalog publications, documents, ordered languages, ISBN identifiers, and Yandex source locations. Run both stages without a terminal using `python scripts/run_daily_maintenance.py`; one worker and the complete candidate cohort are fixed. These tasks are absent from the interactive CLI list. Schedule, configuration, and retention are documented in [operations](operations.md#scheduled-sync-and-cleanup).

Preparation persists plans for non-document formats and publications with known languages that contain no Tatar language. Multilingual publications containing Tatar and publications with unknown language are retained. Files without a source path are skipped. Rerunning preparation refreshes reviewed facts for matching automatic plans that have not entered execution, while preserving their persisted targets. Preparation does not move files, remove remote objects, or delete catalog documents. It needs only the configured `yandex.disk.documents.source_path` and `filtered_out_path`, without storage credentials.

Every duplicate ISBN group requires an explicit review, including groups with a recommended complete PDF. ISBN equality is evidence for review, not proof that editions or files are interchangeable. Publication bibliographic fields and file completeness remain separate facts.

Close the interactive CLI before using review commands; they acquire the same local session lock. Commands print JSON and do not require an interactive terminal:

```bash
python -m app cleanup reviews
python -m app cleanup reviews --status decided --limit 100
python -m app cleanup decide REVIEW_ID --snapshot REVIEW_SNAPSHOT --keep MD5 [MD5 ...]
python -m app cleanup undo REVIEW_ID
python -m app cleanup queue --limit 100
```

Use the `review_snapshot` value from the review listing and full MD5s for every file to retain. The decision checks the inspected snapshot and current publication, file, and source revisions. If facts changed or an older review lacks these facts, rerun preparation and inspect the review again. Cleanup mutation transactions explicitly request read-write mode, even when the primary database defaults to read-only; database and role defaults remain unchanged. Inspection queries inherit the read-only default. Decisions and their cleanup plans commit in one PostgreSQL transaction. Repeating the same decision creates no additional plans. Conflicting decisions fail; undo is allowed only while the exclusive plans remain reversible. Decided reviews remain audit history.

Run progress, detailed logs, summaries, and artifact events use the shared task runtime. Safe stop occurs between persisted items; rerunning reuses active plans and reviews. Cleanup plans, approvals, phases, cancellation evidence, and completion remain durable in PostgreSQL. Attempt counts, run identifiers, and transient errors use local SQLite operational state. Fresh bootstrap creates only the durable columns; upgrading an older schema requires the historical transfer procedure in [operations](operations.md).

The Sync stage executes persisted plans before Yandex discovery and applies ordinary catalog changes in bulk after discovery. Cleanup remains an exception to deferral, as described in [Maintenance guidance](../app/modules/maintenance/guidance/cleanup.md). Daily maintenance requires Yandex and primary-storage configuration. Preparation and review decisions alone do not execute the queue; Sync executes it. Failed plans that reached an execution phase cannot be undone as though execution had never started. Newly discovered documents enter the next run's preparation cohort.

Validation limits and optional coverage follow the [verification policy](verification.md). Static inspection does not establish operational readiness.
