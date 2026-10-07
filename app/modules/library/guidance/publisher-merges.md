# Publisher analysis and review

`library.suggest_publisher_merges` performs one subscription-authenticated Codex analysis of the complete inventory. Generation never changes publishers. Explicit owner apply consumes a durable review draft; the former web review interface is removed.

## Ownership and execution

Library owns inventory/prompt/validation/review intents. PostgreSQL repositories own snapshots, response checkpoints, proposals, revisioned drafts, and pairwise separation decisions. SQLite owns task lifecycle/artifact events only.

- `auto` audits all identities until a valid analysis checkpoint, then unresolved names. `new` includes skipped unresolved names; groups require an unresolved member and at most one established publisher. Preserve its ID and chosen name. `all` permits proposed established-publisher merges. All scopes see active publishers and approved aliases.
- Reserve two PostgreSQL connections: one for the session advisory lock, one for short transactions. Commit after acquiring the lock; release on every exit.
- `codex.publisher_merges` config selects model/reasoning/scope/research/timeout; `codex.executable` may select an absolute CLI path. Authentication uses the service user's file-backed ChatGPT subscription login, with no provider/billing/model fallback.
- The adapter copies only CLI authentication/model metadata into an isolated home, ignores user config/execpolicy, disables shell/image/app/plugin/subagent tools, uses a read-only sandbox, and removes copied credentials afterward. Keyring-only auth is insufficient.
- Exact-model CLI metadata or explicitly verified `context_window_tokens` supplies capacity. Estimate input with o200k_base plus 15% margin and at least one-third capacity reserved for research/reasoning/output. Oversized inventories fail; never split or truncate.

## Response and checkpoints

Compact wire IDs are snapshot-local; map strictly to durable IDs before validation/persistence. Transmit every distinct name/alias without retaining a stale inventory for caching. Keep reported token usage unchanged; token counts do not determine subscription percentages.

The response requires `clusters`, `singleton_ids`, and `unresolved_ids` covering every inventory ID. Reject missing/unknown IDs, old response contracts, and invalid schema/scope/separation claims. Never infer omitted singletons. Preserve singleton/unresolved display names.

Retain overlapping otherwise valid clusters for explicit review; do not join them or select a winner. Derive conflicts from current edits. A publisher cannot be staged in two merges.

Checkpoint a valid completed response before idempotent import. Reruns import remaining checkpoints without inference; interrupted inference needs a later user-started run. Preserve pending/staged proposals and owner edits; deduplicate unchanged groups.

Explicit completed-response recovery runs under the analysis lock and verifies lifecycle completion, current prompt contract, inventory fingerprint, separation decisions, and response. It does not change historical run status. Administrative analysis discard also requires the lock and preserves identities/aliases/audits, manual draft operations, and separation decisions.

## Review transactions

Save name/member edits before staging. Skip makes no identity conclusion; keep-separate immediately persists all member pairs. Discard removes unapplied draft operations/edits and returns staged proposals to review.

Apply rechecks reviewed names, aliases, and identity status transactionally. Unrelated changes/document counts do not invalidate review. Explicit merges reconcile affected separation endpoints and preserve raw-name provenance, aliases, and audits. Singleton keeps/renames use the same draft checks. Raw names must be kept/applied before renaming. No batch undo is provided.

Account telemetry is best effort: match reported bucket/duration/reset; resets, missing readings, and falling usage produce no delta. Five-hour/weekly labels require reported 300/10080-minute windows. Concurrent account activity may contribute to observed usage.

Implementation: `publisher_merge_contract.py`, `publisher_codex.py`, `publisher_merge_review.py`, `publisher_workbench.py`, `app/repositories/publisher_merges.py`. Audit catalog compatibility before relying on this workflow.
