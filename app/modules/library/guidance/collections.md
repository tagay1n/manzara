# Library collections

`library.collection_detect` (Discover collections) is the sole collection task. Select it with `python -m app --task library.collection_detect`, then enter `/run`. It requires one worker and the complete inventory. Discovery is deterministic and saves review proposals; it never calls Gemini, creates collections, changes memberships, overrides inclusion, or rewrites metadata. Validation/apply tasks and the former collection review workbench are removed. No replacement review command is supplied in this slice.

## Catalog input and detection

The shared catalog store reads normalized relations in a repeatable-read snapshot and checks deployed columns, migration revision, and pg_trgm read-only before work. Never repair the schema at task startup or fall back to legacy metadata/collection views.

- The unit of discovery is an unmerged publication with at least one document. Multiple files count as one publication. Publications need present metadata and a usable title; legislation and normalized legal genres are excluded. Inclusion and file privacy are not collection evidence, and discovery changes neither.
- Use bibliographic fields, ordered genres and issue-number subjects, and contextual author/publisher contributions. Resolve a credit through its confirmed active entity only when the contribution and entity are both confirmed; otherwise retain the observed name. Aliases do not establish contextual resolution.
- Paths, filenames, directories, and storage hierarchy are never evidence. The catalog has no independent series-hint field; do not restore missing hints from historical JSON.
- Match canonical collection titles and current member title cores first, with similarity at least 0.72. Equally strong matches to multiple collections retain conflicting target IDs in separate attachment proposals. Already assigned publications are not reassigned.
- Group coherent unmatched publications by normalized near-title cores. Issue markers, periodical type/genres, and publisher consistency contribute to deterministic scores. Shared publisher/author/topic alone does not establish a named collection; proposals require later human review. New-collection proposals need two distinct publications; attachment proposals may contain one.

## Durable proposals and refresh

`catalog_proposals` owns new proposals with `kind=collection`, `status=pending`, and empty `field_changes`. Multi-publication membership is represented in evidence, not the identity-only `catalog_proposal_members` table. `publication_id` on the proposal remains null.

Evidence has `source=catalog.collection_discovery` and a `collection` payload under contract `catalog.collection-discovery.v1`. It records a stable candidate key, input fingerprint, proposal type (`new_collection` or `attach_to_collection`), optional target collection/revision, deterministic score/evidence, and publication snapshots with revisions, represented metadata, document identities, and relevant contextual credits. Candidate identity consists of contract version, proposal type, optional target collection ID, and sorted publication IDs.

A transaction-scoped discovery lock serializes proposal refreshes. Lock source rows and compare the current input snapshot before writing; changed inputs fail with a rerun instruction. Refresh proposals and write catalog revision audits atomically. Identical pending candidates reuse their IDs without revision churn; changed pending candidates refresh evidence. Previously superseded candidates may return to pending. Other decision states remain untouched. Only a complete successful scan supersedes pending candidates that disappeared.

Safe stop or failure before commit rolls back the refresh. Reruns rebuild reproducible features in memory and reuse durable proposals; no feature cache or separate workflow checkpoint is required. A stop arriving after commit does not undo committed proposals.

Historical `library_collection_proposals`, items, signatures, collection events, and validation attempts remain preserved but are not used by discovery. There is no migration, conversion, cleanup, compatibility fallback, or dual write.

## Output and verification

Use `RunContext.log` for redacted terminal output; create no log file, subprocess wrapper, task environment variables, stdout capture, or collection-specific events. The descriptor suppresses shared task lifecycle events, including failure-finalization events. Local run state, progress, heartbeat, stop/recovery, and history remain available. The final `library.collection_discovery_summary` artifact includes scan/candidate counts, proposal refresh counts, proposal IDs, and outcome; its structured `task.artifact` event remains local SQLite data.

Testing follows root `AGENTS.md` and `docs/verification.md`. Static inspection does not establish live terminal behavior, deployed query execution, transaction rollback, or operational readiness.
