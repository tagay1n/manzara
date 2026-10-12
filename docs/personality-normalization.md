# Personality normalization

Owners: `app/modules/library/personality_normalization.py`, `personality_normalization_prompt.py`, `runtime/run_normalize_personalities.py`, `app/repositories/normalization.py`, `app/catalog/personality_candidates.py` / `.sql`, and `app/catalog/personality_normalization.py` / `.sql`. Select `library.normalize_personalities` in the CLI.

## Identity and outcomes

Initials accept one or two alphabetic characters in any script, including `Ja`, `Kh`, `Sh`, `Ts`, `Ju`, with an optional trailing dot. Normalize to an uppercase first letter, a lowercase second letter if present, and one trailing dot. Reject internal dots, spaces, nonalphabetic characters, and strings longer than two letters. Each field represents one initial; the model must not combine several initials in one field. Preserve full names/script; never expand initials.

This initials-only relaxation preserves the existing checkpoint contract versions, so it does not reopen unchanged completed decisions.

Strict responses require every field:

- `normalized`: usable components, null reason; derive canonical name/key locally and atomically persist identity/alias hypotheses, review evidence and durable checkpoint. AI associations stay unconfirmed.
- `not_person`: organization, website, or other non-person.
- `unusable`: unsafe ambiguous/corrupted personal input.

Negatives require a nonblank reason of at most 300 characters and null identity/title/sex fields. Rare names/initials alone do not justify rejection. Valid negatives end fallback; invalid/contradictory responses use bounded content-failure fallback.

## Checkpoints and review

Candidates come from one PostgreSQL query per batch of at most 200 names: joins, document/mention counts, roles, language hints, fingerprints and current durable decisions stay in SQL. Discovery returns durable checkpoint snapshots with candidates; no whole inventory, checkpoint inventory, global counts or per-person PostgreSQL reads are needed. Counts precede language joins so hints cannot multiply mentions. Preserve compact NFC names and the existing compact-JSON SHA-256 fingerprint; canonical display changes must not alter source identity.

`app/repositories/personality_checkpoints.py` composes batch snapshots of PostgreSQL decisions/fingerprints/contracts/accepted identities/conflicts/explicit retries/model hints with local SQLite attempts/content exclusions/recovery evidence. Processing, provider/content failures, deferrals and stop checkpoints are SQLite-only; domain conflicts remain durable. Negatives create no canonical and never rewrite metadata. Successful decisions with unchanged source fingerprints skip in SQL regardless of prompt/schema version; unchanged current-contract negative decisions also skip. Local retry/content exclusions apply to the returned batch and do not consume the candidate limit. Changed sources or explicit persisted `retry_requested` reopen successful decisions without erasing evidence; changed prompt/schema versions also reopen negative decisions. Older successful decisions remain accepted without rewriting their checkpoints. No CLI review/retry workbench exists.

Each validated decision uses one PostgreSQL simple-query message and an implicit atomic transaction, including source/checkpoint guards, identity matching, hypotheses, reviews, revisions and the durable checkpoint. Identity ambiguity records a failed checkpoint inside that same command. Stale observations defer locally without replacing newer domain decisions. Failed/uncertain command sessions are discarded and never replayed automatically; fresh discovery reconciles durable truth. SQLite cleanup retains attempt evidence rather than copying the committed decision.

Preserve `recovered_response` / `previous_failure` evidence; item HTTP 400 closes pending recoveries and records blocking evidence. Never reopen every failure automatically.

Reuse an exact-compatible active identity, preferring the checkpoint target; otherwise ambiguity requires review or a new unconfirmed identity is created. Preserve human approvals/reviewed associations. Multiple active alias targets require explicit resolution; normalization never resolves contributions.

Check review ownership before alias insertion. If its trigger creates a new owner-labeled placeholder, fill it with AI evidence transactionally; restore pre-existing human review fields changed by the alias trigger.

## Queue and reporting

Untouched names precede checkpointed names before the limit, using a start-of-run snapshot of local history names. One sequential loop loads bounded batches, excludes already visited names and drains at most one delayed retry per selected name after the entire first pass. Empty discovery or reaching the candidate limit closes the first pass; retries consume no further discovery queries. Yield on 429, service 5xx, transport/local deadline failures after recording shared provider state, without content-excluding the model. Wait stoppably when capacity is unavailable; total daily exhaustion preserves untouched work. A second transient failure defers locally; stop preserves pending retries.

[Gemini runtime](gemini-runtime.md) owns personality pacing and transport. `/settings` edits this task's independent `gemini.request.personality_timeout_seconds` response deadline from 1 to 900 seconds in private YAML; the current configured value is retained. The run snapshots it before processing and uses the shared generation/stream cancellation helper. Preparation, provider admission/spacing and cleanup stay outside the deadline.

Count unique people separately from physical `model_attempts`, distinguishing negatives, failures, final deferrals, and pending retries. Report discovered/eligible counts as they become known; do not infer the whole catalog's total or remaining count when stopped or limited. Validated result JSON is pretty-printed through the shared redacted task logger before the save, followed by an explicit persistence outcome; invalid-response diagnostics remain bounded. Safe stop prevents new claims and finishes checkpoint boundaries; shared logging/progress/artifacts retain stopped/deferred outcomes.
