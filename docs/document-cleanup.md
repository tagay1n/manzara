# Document cleanup

The CLI's **Cleanup plan** task (`library.prepare_document_cleanup`) reads normalized catalog publications, documents, ordered languages, ISBN identifiers, and Yandex source locations. Launch with `python -m app --task library.prepare_document_cleanup`, then choose Start / Resume. Use one worker and no candidate limit: duplicate detection needs the entire cohort.

Preparation persists plans for non-document formats and publications with known languages that contain no Tatar language. Multilingual publications containing Tatar and publications with unknown language are retained. Files without a source path are skipped. Preparation does not move files, remove remote objects, or delete catalog documents. It needs only the configured `yandex.disk.documents.source_path` and `filtered_out_path`, without storage credentials.

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

Run progress, detailed logs, summaries, and artifact events use the shared inline task runtime. Safe stop occurs between persisted items; rerunning reuses active plans and reviews. Cleanup plans, approvals, phases, cancellation evidence, and completion remain durable in PostgreSQL. Attempt counts, run identifiers, and transient errors use local SQLite operational state. Fresh bootstrap creates only the durable columns; upgrading an older schema requires the historical transfer procedure in [operations](operations.md).

Remote plan execution remains in the disabled Maintenance sync workflow, pending a separate catalog/runtime adaptation. The planning and review commands do not execute the queue. Required remote safety behavior is recorded in [Maintenance guidance](../app/modules/maintenance/guidance/cleanup.md).

Validation uses syntax/import inspection, read-only catalog inspection when reachable, and diff review. No tests or credential-backed cleanup runs were performed; static inspection does not establish operational readiness.
