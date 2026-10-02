# Publisher merge analysis

`library.suggest_publisher_merges` is a Library catalog task. It performs one
subscription-authenticated Codex analysis of the complete inventory. Generation
never changes publishers. Only the Publishers page's explicit Apply changes
operation consumes the durable owner draft.

The Library owns inventory, prompt, response validation, and review intents.
The repositories own PostgreSQL snapshots, completed-response checkpoints,
group proposals, the revisioned review draft, and stable pairwise separation
records. Local SQLite retains task lifecycle and artifact events only. Files
remain below the configured artifacts root.

`auto` audits all identities until a valid analysis checkpoint exists, then
examines unresolved names. `new` includes skipped unresolved names; each group
needs an unresolved member and at most one established publisher. Adding aliases
preserves the established ID and chosen name. `all` explicitly permits proposed
merges among established publishers. Each scope sees every active publisher and
approved alias. An absent suggestion does not establish uniqueness.

The forward review-edit migration ensures the nullable `review_edit` JSONB column
exists on both fresh installations and earlier deployed proposal tables, preserving
all proposals and owner drafts.

The runner reserves a minimum pool of two PostgreSQL connections: one holds the
session advisory lock, and one remains available for short catalog/checkpoint
transactions. This is a task-specific exception to TaskRunner's one-connection
subprocess default. The lock session commits immediately after acquiring the
lock, so analysis does not leave a transaction idle for its duration. Lock release
runs on success and failure; PostgreSQL also releases it if the session closes.

The local config's `codex.publisher_merges` settings select model, reasoning,
scope, research, and timeout. No model or billing fallback is permitted. CLI auth
must use ChatGPT; run `codex login` as the task service user. Set
`codex.executable` to the installed CLI's absolute path when its directory is
absent from the task service's `PATH` (common with user-local npm installs).
The adapter copies
only CLI file-backed authentication and model metadata into an isolated home,
removes that copy after the run, ignores user config and execpolicy, disables
shell, image, app, plugin and subagent tools, and uses the read-only sandbox.
Keyring-only authentication requires a file-backed CLI login for the service.
The installed CLI must support the checked execution flags and feature controls.
Local input size uses the o200k_base reference tokenizer with a 15% margin,
plus a research/reasoning/output reserve of at least one third of capacity. This
is a local estimate, distinct from reported usage; tokenization can differ by
model. Tokenizer downloads are cached below the task workspace.
Model capacity comes from exact-model local CLI metadata, or an explicit verified
`context_window_tokens`. Oversized inputs fail; they are never split or truncated.

Prompt v3 transmits compact rows with snapshot-local string IDs and each name
once per identity. The first name remains the chosen name; all distinct aliases
are retained. Responses are strictly mapped back to durable IDs before domain
validation and checkpointing; checkpoints and owner review never store wire IDs.
The saved inventory snapshot deterministically reconstructs the wire mapping.

Stable instructions and the complete identity inventory precede changing scope,
row-aligned distinct document counts, and separation pairs. Identical inventories
therefore retain their prompt prefix when only these settings change. CLI execution
uses an empty sibling `codex-context` directory with a stable path across runs;
all credentials, outputs, and logs remain in their dedicated run workspace.
Inventory edits can still change the prefix and snapshot-local IDs. No stale
inventory is retained to improve cache reuse. Reported token usage, including
cached input when supplied by the CLI, is retained unchanged. Cache reuse and
subscription quota savings are not guaranteed; API cache prices do not establish
the subscription quota formula. See the
[official prompt caching guidance](https://developers.openai.com/api/docs/guides/prompt-caching).

The strict wire response requires `clusters` (matching groups with `kind: cluster`),
`singleton_ids` (explicit standalone assignments), and `unresolved_ids` (explicit
unresolved assignments). Compact ID arrays avoid repeating thousands of long
source names and per-entry boilerplate in model output. The backend maps only
these explicit assignments to their verbatim source names and labeled records;
no missing ID is synthesized as a singleton. Every
inventory ID must appear. Missing coverage, unknown IDs, old `groups` responses,
and incomplete records fail validation; omitted entries are never inferred to
be singletons. Singleton and unresolved records preserve their source display
name. Matching clusters may propose a researched standardized publisher name;
`new` scope still preserves an established publisher's owner-chosen name.
The response list uses the existing durable JSON tables; no data conversion,
schema migration, or previous-response fallback is part of this change.

The review displays proposed publisher names with the union of member names and
approved aliases, excluding the chosen name, as proposed aliases. Coverage and
filters distinguish clusters, singleton publishers, and unresolved entries.
A singleton can be explicitly staged as a keep for a raw name, or a rename of
an existing canonical. An unchanged canonical already needs no approval.
Owner edits may reduce a cluster to one entry before keeping it. These keeps
and renames share the durable draft's snapshot checks and apply/discard boundary.

Groups must still pass individual schema, identity, scope, and separation checks.
Overlap between otherwise valid groups is retained for owner review, never resolved
by joining groups or selecting a winner. The backend derives conflicts from the
current proposal edits, lists conflicting groups first, and exposes shared member
IDs and related proposal IDs. Both groups remain pending until explicit owner
review. Removing a shared member clears the conflict; the same publisher cannot
be staged in two merges. The model is still asked for disjoint groups.

A valid completed response is checkpointed before idempotent import. A later run
imports a remaining checkpoint without another analysis. Interrupted inference
requires a later user-started run. Pending and staged proposals survive reruns;
unchanged pending groups are deduplicated without replacing owner edits.
`recover_completed_response` supports explicit recovery of a saved completed CLI
response without inference. It checks the persisted analysis state, prompt version,
exact inventory and fingerprint, final lifecycle completion, current separation
decisions, and response validation before checkpointing and idempotent import.
Recover under `publisher_analysis_lock`; no adapter or account telemetry is started.
The historical failed run stays failed; recovery does not rewrite its history.
Recovery accepts only the current prompt contract. The owner explicitly discarded
the previous merge-suggestion analyses and proposals; their identities, aliases,
audits, manual draft operations, and separation decisions remain intact. The
administrative `discard_publisher_analyses` operation removes generated data and
proposal-backed staged operations, preserving manual draft operations. It must
run under the analysis lock. Old responses are not converted or reused.

Name and member edits are saved on the backend before staging. Stage merge
adds the edited group to the durable apply draft. Skip records no identity
conclusion. Keep separate records all pairs of the edited members immediately.
Discard clears unapplied draft operations and group edits and restores staged
proposals to review.
Apply checks reviewed names, aliases and identity status inside the transaction;
document-count changes and unrelated catalog updates do not invalidate review.
Explicit owner merges reconcile only affected separation endpoints, retaining
raw-name provenance. Alias retention and publisher audit events remain intact.
Batch undo is not provided by this feature.

Account telemetry uses `account/rateLimits/read` best-effort. Windows are matched
by metered bucket, reported duration and reset time. Resets, missing readings,
and falling usage produce no delta. Five-hour and weekly labels are used only
for reported 300-minute and 10080-minute windows. Percentages describe account
usage observed during the run, at reported precision, with concurrent activity
possibly contributing. Token counts never determine subscription percentages.

Official interfaces verified for installed CLI 0.159.3:
[non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode),
[configuration](https://learn.chatgpt.com/docs/config-file/config-reference), and
[account telemetry](https://learn.chatgpt.com/docs/app-server).
No exact version requirement is imposed.
