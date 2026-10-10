# Publisher clustering proposals

`library.suggest_publisher_merges` is an interactive CLI task that performs one subscription-authenticated Codex analysis of the complete publisher inventory. It saves proposals only. CLI review, editing, staging, separation, and apply commands remain deferred; the former web interface is removed. Retained workbench/review helpers are not execution dependencies of this task and still require separate catalog adaptation.

## Inventory and scope

The catalog store reads normalized entities, names, linked alias reviews, and publisher contributions. Preserve the previous inventory grouping rule: one entry per active publisher, containing its display name and aliases marked `linked` to that identity. Only observed spellings outside the combined name set become new candidates. Human confirmation and individual credit resolution do not change this grouping rule. Generation never resolves credits or changes identities, aliases, or confirmation.

All scopes see every active publisher classified by the existing canonical projection rules. Included library publications with metadata supply observed spellings and distinct MD5 counts; aliases do not double-count documents. An uncovered spelling is analyzed once, retaining every associated catalog name ID. Durable members use `entity:<id>` or `name:<id>` keys. Compact prompt IDs remain snapshot-local.

`auto` uses `all` until a valid checkpoint with the current catalog contract exists, then `new`. `new` includes skipped unresolved names; clusters need a new member and at most one existing identity, preserving its ID and chosen name. `all` permits proposed existing-identity merges. Empty inventories and `new` scopes without new candidates finish without inference. The task requires exactly one worker and no candidate limit; never split or truncate an oversized inventory.

## Runtime and configuration

The flow uses the shared `RunContext` database, terminal logging, progress, cancellation, and artifact contracts. Reserve at least two shared PostgreSQL connections: one for the session advisory lock and one for short transactions. Commit after acquiring the lock and release it on every exit. Preflight checks the deployed catalog read-only; migration remains a separate operation.

`codex.publisher_merges` config selects model, reasoning, scope, research, timeout, and optional verified context capacity; `codex.executable` may select an absolute CLI path. Authentication uses the service user's file-backed ChatGPT subscription login with no provider, billing, or model fallback. The adapter copies only authentication/model metadata into a private isolated home, ignores user configuration/rules, disables local shell/image/app/plugin/subagent tools, uses a read-only sandbox, and removes copied credentials on every exit. Keyring-only authentication is insufficient.

Exact-model CLI metadata or verified `context_window_tokens` supplies capacity. Estimate input with o200k_base plus 15% margin and reserve at least one-third capacity for research/reasoning/output. Tokenizer caching uses a separate process environment and the run workspace; never mutate the interactive process environment.

Logs appear only through the CLI's redacted terminal sink. There is no publisher `.log` file, raw stdout forwarding, web review link, or SSE emission. Retain inventory, prompt, response schema, response, redacted lifecycle JSONL, and bounded structured diagnostics under the configured artifacts root. The subprocess drains both output pipes with bounded buffers. SQLite owns coalesced progress, run lifecycle, and persisted `task.artifact` events. Saved summaries contain counts, analysis ID, and paths; detailed proposals live in `proposals.json`, referenced by the final artifact event, rather than in the local run row or final terminal line.

## Response, persistence, and recovery

Require `clusters`, `singleton_ids`, and `unresolved_ids` to cover every inventory ID. Reject missing/unknown/duplicate group IDs, singleton/unresolved overlaps, incompatible entity kinds, invalid scope or separation claims, and proposals exceeding 200 catalog members. Never infer omitted singletons. Preserve singleton/unresolved names. Retain otherwise valid overlapping clusters for explicit later review; do not join them or select a winner.

PostgreSQL owns `publisher_merge_analyses` response checkpoints and catalog proposal/member snapshots. `catalog_proposals` remains the sole proposal owner; `catalog_publisher_proposals` projects those records without duplicate domain rows. Proposal insertion writes transactional audit records and preserves entity/name snapshots and reviewed entity revisions. It does not initialize or modify review drafts.

Checkpoint a valid completed response before atomic, idempotent import. Recheck current identity snapshots, linked-name associations, and separation decisions under the catalog review lock, including decisions for suppressed alias spellings. Unrelated document-count changes do not invalidate identity review. Preserve pending/deferred proposals and owner edits, deduplicating unchanged groups with the same member snapshots, kind, and proposed name. Different proposed names for overlapping members remain separate proposals. Changed identities or decisions reject the checkpoint with an actionable failure; no partial proposal import is committed. Contradictions between linked-name grouping and separation decisions fail before inference and require association review.

Only analyses tagged `catalog.publisher-clusters.v1` are eligible for replay or `auto` history. Historical analyses, proposals, drafts, and decisions remain unchanged and are not translated or reused. Abandonment/rejection updates apply only to the current contract. No persisted-data migration is required.

Cancellation stops Codex and returns a stopped task result without importing incomplete output. A committed valid checkpoint can be imported by the next user-started run. Timeouts, provider failures, and invalid responses fail visibly without automatic reinference. Explicit internal completed-response recovery verifies the analysis contract, prompt version, inventory fingerprint, successful lifecycle/diagnostics, response, and current catalog under the analysis lock; it never rewrites historical task status.

Account telemetry is best effort: match reported bucket/duration/reset; resets, missing readings, and falling usage produce no delta. Five-hour/weekly labels require reported 300/10080-minute windows. Concurrent account activity may contribute to observed usage. Keep reported token usage unchanged; token counts do not determine subscription percentages.

## Verification boundary

Static inspection does not establish terminal behavior, authenticated Codex availability, safe stopping, live catalog transactions, or checkpoint recovery. These remain pending runtime acceptance. Testing follows the owner's explicit-request policy in root instructions.
