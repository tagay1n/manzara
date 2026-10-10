# Publisher clustering proposals

`library.suggest_publisher_merges` performs one subscription-authenticated Codex analysis of the complete publisher inventory. It saves proposals only; review/edit/stage/separate/apply commands and workbench are absent. Reject candidate limits and never split/truncate oversized input.

## Inventory and scope

Read normalized entities/names/linked alias reviews/publisher contributions. Group one entry per active publisher with display name and linked aliases; only spellings outside the combined name set become candidates. Confirmation/contextual resolution do not change grouping. Generation never changes identities/aliases/confirmation/credits.

All scopes include every active publisher under canonical projection rules. Included publications with metadata supply observed spellings/distinct MD5 counts; aliases do not double-count. Analyze uncovered spellings once with every associated name ID. Durable keys are `entity:<id>` / `name:<id>`; prompt IDs are snapshot-local.

`auto` uses `all` until a current-contract checkpoint exists, then `new`. `new` includes skipped unresolved names and requires a new cluster member plus at most one existing identity, preserving its ID/name. `all` permits existing-identity merge proposals. Empty inventory/no new candidates finishes without inference.

## Runtime and configuration

Reserve at least two PostgreSQL connections for the session advisory lock and short transactions; commit after acquiring and always release. Preflight checks catalog read-only.

`codex.publisher_merges` owns model/reasoning/scope/research/timeout/nullable capacity; `codex.executable` selects CLI and `codex.home` subscription auth/model metadata. `codex.transport` owns deadlines/buffers/tokenizer margin/reserved context. File-backed service-user ChatGPT login is required; keyring-only auth and provider/billing/model fallback are unsupported. Copy only auth/model metadata to a private isolated home, ignore user rules/config, disable shell/image/app/plugin/subagent tools, use read-only sandbox, remove credentials on every exit.

Exact-model CLI metadata or verified `context_window_tokens` supplies capacity. Estimate with o200k_base plus margin/reserved research/reasoning/output. Tokenizer caching uses separate subprocess environment/workspace, never mutates the interactive environment.

Drain both subprocess pipes into bounded structured diagnostics without raw forwarding. Retain inventory/prompt/schema/response/diagnostics in the artifact workspace; shared runtime owns logs/progress/cancellation. Summary stores counts/analysis ID/paths; detailed `proposals.json` is linked from final artifacts, not dumped into run rows/logs.

## Response and recovery

`clusters`, `singleton_ids`, `unresolved_ids` must cover all inventory IDs. Reject missing/unknown/duplicate group IDs, singleton/unresolved overlap, incompatible kinds, invalid scope/separation claims, and >200 members per proposal. Never infer omitted singletons. Preserve singleton/unresolved names and otherwise-valid overlapping clusters for review without joining/selecting winners.

PostgreSQL `publisher_merge_analyses` owns response checkpoints; `catalog_proposals` owns proposal/member snapshots and audits, projected by `catalog_publisher_proposals`. Generation never changes review drafts.

Checkpoint validated output before atomic idempotent import. Under catalog review lock recheck identity snapshots, linked names, and separation decisions, including suppressed aliases. Document-count changes alone do not invalidate identity review. Preserve owner edits/pending/deferred proposals; deduplicate unchanged member snapshots/kind/name. Different names for overlapping groups stay separate. Changed identities/decisions reject checkpoint without partial import; grouping/separation contradictions fail before inference and need association review.

Only `catalog.publisher-clusters.v1` analyses qualify for replay/auto/abandonment/rejection. Historical records remain untouched and unused. Cancellation stops Codex without importing incomplete output; committed responses resume on the next user-started run. Timeout/provider/invalid-response failures do not automatically reinfer.

Telemetry is best effort: match reported bucket/duration/reset; missing/reset/falling readings produce no delta. Five-hour/weekly labels require 300/10080-minute windows. Concurrent usage may contribute; retain reported tokens without deriving subscription percentages. Readiness limits: [verification](../../../../docs/verification.md).
