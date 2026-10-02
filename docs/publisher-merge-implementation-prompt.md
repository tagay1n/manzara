# Implement: Suggest publisher merges

Implement the feature specified below in Manzara. This is an implementation
handoff for another Codex model, not a request to produce another plan. The owner
has agreed to the product decisions and additive PostgreSQL migration policy
below. Work through the implementation and verification within that scope.

Read the root and nearest applicable `AGENTS.md` files, `docs/architecture.md`,
the Library navigation guidance, and `docs/verification.md` before editing.
Use TDD for behavior changes. Preserve module ownership and update relevant
guidance in the same change.

## Latest owner direction

The owner replaced selected merge suggestions with complete publisher clustering.
Codex must account for every inventory ID in a response with `clusters`,
`singleton_ids`, and `unresolved_ids`. Matching clusters have kind `cluster`;
explicit singleton/unresolved IDs retain their source names. Matching clusters may propose
a researched general publisher name. Existing names remain verbatim aliases.
Singleton/unresolved records retain their single source display name. Existing
owner-chosen canonical names remain protected in `new` scope. The review shows
proposed names and aliases, complete coverage, and separate category filters.
The owner authorized deleting previous generated analyses and suggestions rather
than converting them. Preserve publisher identities, approved aliases, audits,
manual drafts and separation decisions. Do not migrate old data, fabricate missing
singleton results, reuse old responses, or provide an old-format fallback.
This direction supersedes narrower merge-only requirements below.

## 1. Goal and agreed behavior

Add **Suggest publisher merges** under **Library catalog**, with task ID
`library.suggest_publisher_merges`.

The owner reported 3,774 distinct publisher entries during planning; this is
context, not a hardcoded count or limit. Names may represent the same publisher
in Cyrillic, Zamanalif, Yanalif, abbreviated form, or slightly different spelling.

The task sends one complete publisher inventory to Codex using the owner's
ChatGPT subscription login. It saves merge proposals for review on the existing
Publishers page. Publisher changes require the owner's explicit **Apply changes**
action. Generation must never merge, rename, split, or delete publishers.

Agreed requirements:

- Compare names across scripts, abbreviations, and spelling variants.
- Treat historical renames, imprints, subsidiaries, and parent organizations
  conservatively. A relationship does not by itself establish identical identity.
- Allow web research, with citations for claims relying on external evidence.
- Include uncertain groups, clearly labeled and ordered after stronger matches.
- Prefer existing names; preserve original spellings and scripts as aliases.
- Keep explanations and interface text in English. Retain source names verbatim.
- Persist review progress across reloads.
- Keep the interface compact; the owner explicitly rejected a complex dashboard.
- Perform one Codex analysis session per run, without an automatic verification
  pass. Web research may involve multiple internal model calls.

## 2. Future discoveries and protection of owner decisions

Approved publishers form a durable catalog. Each canonical publisher retains
its ID, chosen display name, aliases, and audit history.

Handle incoming names as follows:

- An exact approved alias resolves through its existing mapping without another
  model decision.
- An unfamiliar variant remains unresolved until the owner approves a proposed
  association.
- A genuinely separate publisher can be established using the existing **Keep**
  action.
- Absence from model suggestions does not establish uniqueness.

Introduce configurable analysis scope:

| Scope | Behavior |
| --- | --- |
| `auto` | Perform a full audit until the first successful analysis is saved; subsequent runs examine unresolved names. |
| `new` | Examine all currently unresolved names, including previously skipped names. |
| `all` | Explicitly allow another full audit, including proposed merges between established publishers. |

Every scope receives the complete inventory, including established publishers
and all aliases. Scope controls which proposals are eligible, not which
publishers the model can see.

In `new` scope, every proposed group must contain at least one unresolved entry
and at most one established publisher. Compare unresolved names against each
other and the established catalog. A new name must not become a bridge for
merging established publishers.

Adding approved aliases to an established publisher must preserve its ID and
chosen name. A rename requires an explicit owner edit. Codex must never remove
approved aliases, split established groups, or replace prior decisions.

A later full audit may propose merging established publishers, but cannot
execute that merge. Previously pending proposals remain available for review
independently of the current generation scope.

If `new` scope finds no unresolved names, finish successfully without inference
and report that no analysis was needed.

## 3. Codex configuration and execution

Add configuration:

```yaml
codex:
  executable: codex
  publisher_merges:
    model: gpt-6.1-sol
    reasoning_effort: medium
    scope: auto
    web_search: true
    timeout_seconds: 3600
```

Use normal local configuration loading and update `config.example.yaml`. Never
load the example at runtime. Validate values strictly, including boolean values
and integral positive timeouts. Resolve model and reasoning settings from
configuration; do not silently switch models or billing methods.

`gpt-6.1-sol` with medium reasoning is the initial recommendation, subject to
account availability. No Tatar accuracy benchmark is assumed. The model must
remain configurable without code changes.

Use a Python subprocess adapter around the installed Codex CLI. The planning
environment had CLI version `0.159.3`; verify the actual installed version and
supported options before implementation. Do not hardcode that version as a
requirement without a demonstrated dependency.

- Require ChatGPT subscription authentication and give actionable errors for
  missing authentication or unavailable models.
- Use an isolated workspace under the configured artifacts root.
- Supply instructions and inventory directly, avoiding unrelated repository
  context, integrations, and credentials. Do not expose application database or
  storage credentials to the model process.
- Enable web search while disabling unnecessary local execution and editing
  capabilities through supported configuration. Confirm the controls against
  the installed CLI rather than inventing flags.
- Request structured output and consume machine-readable lifecycle events.
- Permit one active analysis for this task.
- Support cancellation, timeout, and clean child-process termination.
- Keep publisher prompts and rules in the Library module. Avoid introducing a
  general provider framework or changing existing Gemini workflows.

## 4. Complete inventory, evidence, and proposal validation

Build a deterministic inventory of active canonical publishers and unresolved
raw names. Include stable entry identifiers, display names, all aliases,
canonical/unresolved status, and distinct document counts. Keep a mapping to
existing publisher keys.

Persist the inventory snapshot and fingerprint. Do not silently truncate
inventory queries or duplicate linked aliases as unresolved entries. Count
distinct documents rather than summing overlapping alias counts.

Assess input size against model context capacity, reserving space for
instructions, research, reasoning, and output. If the whole inventory cannot
fit, stop with an actionable explanation; do not silently split or truncate it.
Clearly distinguish any local token estimate from service-reported usage.

The model prompt must require coherent, disjoint groups supported by identity
evidence. Script similarity or acronym expansion alone is insufficient. Treat
inventory and web content as data, respect recorded separation decisions,
distinguish facts from uncertainty, and avoid claims that ungrouped names are
proven unique. Do not combine a chain of weak pairwise similarities into a group
without evidence of one shared identity.

Use the inventory first and research ambiguous identities when useful,
preferring authoritative sources. Record source URLs and explain what each
external source supports. Allow uncertainty when evidence is inconclusive.

Each proposal contains member IDs, a proposed existing name, rationale,
confidence category, uncertainty, and citations. Confidence categories order
review priority; do not present uncalibrated numerical probabilities as facts.

Validate all fields and scope rules before publishing. Reject unknown IDs,
fewer than two members, duplicate members, invalid types,
invented proposed names, unsafe citation schemes, and conflicts with separation
decisions. A proposed name must follow the established-publisher preservation
rule in `new` scope. Owner edits during review may choose a different name.
Owner update: retain otherwise-valid overlapping groups for manual review instead
of failing the complete response. Show shared publishers and competing proposals,
list conflicts first, and keep staged merges disjoint. Do not automatically pick
a winner, join groups, or run inference again to resolve overlaps. Saved completed
responses may be explicitly recovered after matching their persisted inventory.

Checkpoint a completed valid response before importing proposals. Resume
validation/import from that checkpoint without repeating inference. Interrupted
generation may restart on a later user-started run; never publish incomplete
output as a completed analysis. Import must be idempotent.

## 5. Usage reporting and task artifacts

Record configured model, reported model where available, reasoning effort,
CLI and prompt versions, duration, and reported token usage.

Capture account usage before and after analysis through a best-effort Codex
app-server telemetry adapter. Use the documented `account/rateLimits/read`
interface and isolate its version-dependent behavior from generation.

- Identify windows by reported duration and metered bucket, not by assuming
  that the primary and secondary fields always mean five-hour and weekly.
- Report five-hour and weekly percentages, reset times, and percentage-point
  changes only for matching buckets and comparable windows.
- Label these as **account usage observed during this run**, not exact task
  attribution. Concurrent Codex activity can contribute to the change.
- Handle resets, missing windows, reporting precision, and unavailable telemetry
  explicitly. Do not fabricate a zero or a comparable delta when unavailable.
- Never calculate subscription percentages from token counts.
- Telemetry failure must not invalidate valid proposals.

For example, a comparable five-hour window moving from 12% to 15% may be shown
as a 3 percentage-point increase during the run. It must not be claimed as
exact consumption attributable solely to this task.

Emit a compact structured task summary containing proposal counts, publishers
involved, scope, model, duration, token usage, quota observations, and a review
link. Use existing artifact events and summaries rather than log parsing. Keep
the dedicated structured artifact log and bounded log pagination.

## 6. Durable storage and simple review

Use an additive PostgreSQL migration for analysis snapshots, group proposals,
durable review drafts, and separation decisions. The owner has explicitly
approved this migration policy. Preserve existing publishers, aliases, audit
history, and the earlier migration that removed retired publisher suggestions.
Do not repurpose the retired per-alias suggestion flow as group storage.

Durable review and recovery information belongs in PostgreSQL. Disposable
runtime state belongs in local SQLite. File artifacts belong under the
configured artifacts root. Do not introduce cross-store foreign keys or
dual-write domain state.

Add a compact **Merge suggestions** section to the existing Publishers page:

- Show one group at a time with a position indicator.
- List member names and aliases, an editable canonical name, and controls to
  remove members.
- Show a short rationale and an **Uncertain** label where applicable.
- Keep citations expandable and reuse existing source-document links.
- Offer **Stage merge**, **Keep separate**, and **Skip**.
- Retain the existing **Apply changes** and **Discard** controls.

Save publisher review drafts on the backend, including staged merges and edits.
Reload restores the draft. Discard clears unapplied changes and returns
corresponding proposals to review. The browser owns only transient interaction
and rendering state.

**Keep separate** records pairwise separation between the reviewed members.
The owner edits a partially correct group before making this decision. Skip
records no identity conclusion.

Separation decisions must survive routine document-count changes, canonical
renames, and alias additions. Tie them to stable identities, retaining raw-name
provenance where needed. When explicit owner-approved merges change identities,
reconcile associated separation records transactionally; never erase unrelated
decisions.

Reruns must preserve drafts and decisions, deduplicate unchanged pending
proposals, and add new valid groups. They must not overwrite owner edits.

Add interfaces to list proposals, manage the durable draft, and record
separation decisions. Extend the existing apply operation to consume the
server-owned draft and atomically record proposal outcomes alongside publisher
merges. Preserve existing manual callers.

At apply time, validate affected members and aliases against reviewed snapshots
within the write transaction. Reject stale or conflicting changes with an
actionable refresh message. Unrelated catalog changes must not invalidate a
reviewed group.

Do not rely on the existing `snapshot_token` alone: the publisher apply behavior
inspected during planning does not enforce it. Preserve alias retention and
audit events. Do not assume publisher batch undo already works.

## 7. Tests and completion criteria

Add focused failing tests before implementation for:

- Complete inventory export, all aliases, stable identifiers, and distinct
  document counts.
- Cross-script and acronym fixtures, uncertain groups, citations, and strict
  response validation.
- First-run full audit, subsequent unresolved-name analysis, explicit full
  audit, and no-inference empty runs.
- Exact-alias reuse and adding a new alias without changing an established ID
  or display name.
- Preservation of approved catalog data across repeated runs.
- Separation decisions surviving renames, alias additions, and document-count
  changes.
- Configurable model/reasoning, subscription authentication, and no silent
  provider fallback.
- Missing CLI, context overflow, service failures, invalid output, cancellation,
  and timeout.
- Checkpoint recovery without repeated inference or duplicate proposals.
- Durable drafts, editing, skip, separation, discard, and explicit apply.
- Stale affected members, unrelated changes, transaction rollback, and alias
  preservation.
- Comparable quota windows, resets, multiple buckets, unavailable telemetry,
  and missing token fields.
- Keyboard-accessible review controls and safe rendering of model content and
  links.

Mock Codex and telemetry in automated tests. Do not consume subscription
allowance or merge real publishers during development.

Run focused checks followed by required backend, frontend, lint, and migration
checks. Update task documentation, configuration examples, navigation guidance,
and the Library guidance that currently restricts models to Gemini.

Completion means the owner can review the initial catalog, preserve every
approved identity, and later rerun the same task to match newly discovered
names against that established catalog. Report what changed, verification
results, and any concrete limitations.

## Official references

These references were consulted during planning. Verify current supported
interfaces when implementing; account access must be checked locally.

- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex authentication](https://learn.chatgpt.com/docs/auth)
- [Codex models](https://learn.chatgpt.com/docs/models)
- [Codex app-server and telemetry](https://learn.chatgpt.com/docs/app-server)
- [Subscription usage and pricing](https://learn.chatgpt.com/docs/pricing)
