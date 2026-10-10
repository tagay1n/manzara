# Collection discovery

`library.collection_detect` requires the complete inventory and makes deterministic review proposals. It never calls AI, creates collections, changes membership/inclusion, or rewrites metadata. Validation/apply and the review workbench are absent.

## Input and detection

Read normalized relations in a repeatable-read snapshot after read-only schema/revision/pg_trgm preflight. Never repair schema at startup or fall back to legacy views.

- Unit: unmerged publication with documents, present metadata, usable title; exclude legislation/normalized legal genres. Multiple files count once. Inclusion/privacy are not collection evidence.
- Evidence: bibliography, ordered genres/issue-number subjects, contextual author/publisher credits. Use resolved entities only when both contribution and active entity are confirmed; otherwise retain observed names. Aliases do not resolve contextual credits.
- Never use paths/filenames/directories/storage hierarchy or resurrect historical series hints.
- Match canonical collection titles/member title cores at similarity >=0.72 first. Equal matches retain separate conflicting target proposals; never reassign existing members.
- Group coherent unmatched near-title cores. Issue markers, periodical genres/type, and publisher consistency affect scores; shared publisher/author/topic alone is insufficient. New proposals need two publications; attachments may have one. All require human review.

## Proposals and refresh

`catalog_proposals` owns `kind=collection`, `status=pending`, empty `field_changes`, null `publication_id`. Multi-publication membership lives in evidence, not identity-only `catalog_proposal_members`.

Evidence source `catalog.collection_discovery`, contract `catalog.collection-discovery.v1`, records candidate key/fingerprint, type (`new_collection` / `attach_to_collection`), optional target/revision, scores/evidence, and publication revision/metadata/document/contextual-credit snapshots. Identity comprises contract, type, optional target ID, and sorted publication IDs.

A transaction-scoped discovery lock serializes refresh. Lock/recheck source snapshots; changed input fails with rerun guidance. Refresh/audit atomically. Identical pending candidates reuse IDs without revision churn; changed candidates refresh; superseded candidates can return to pending. Other decisions stay intact. Only a complete successful scan supersedes disappeared pending candidates.

Stop/failure before commit rolls back; stop after commit preserves proposals. Rebuild features in memory on rerun, without feature caches/checkpoints. Historical collection proposals/items/signatures/events/validation attempts remain untouched and unused; no migration/fallback/dual-write.

## Output

Use shared `RunContext` logging/progress/artifacts, without wrappers, environment mutation, or stdout capture. `library.collection_discovery_summary` records scan/candidate/refresh counts, proposal IDs, and outcome; save JSON and link the local summary. [Verification](../../../../docs/verification.md) owns readiness limits.
