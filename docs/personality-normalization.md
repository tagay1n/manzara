# Personality normalization

Owners: `app/modules/library/personality_normalization.py`, `personality_normalization_prompt.py`, `runtime/run_normalize_personalities.py`, `app/repositories/normalization.py`, and `app/catalog/personality_normalization.py`. Select `library.normalize_personalities` in the CLI.

## Identity and outcomes

Initials accept one alphabetic character or Latin `Kh`, `Sh`, `Ts`, `Ju`, normalized to an uppercase first letter and trailing dot. Reject internal dots, multiple initials, and other multi-letter strings. Preserve full names/script; never expand initials.

Strict responses require every field:

- `normalized`: usable components, null reason; derive canonical name/key locally and atomically persist identity/alias hypotheses, review evidence, audit, and durable checkpoint. AI associations stay unconfirmed.
- `not_person`: organization, website, or other non-person.
- `unusable`: unsafe ambiguous/corrupted personal input.

Negatives require a nonblank reason of at most 300 characters and null identity/title/sex fields. Rare names/initials alone do not justify rejection. Valid negatives end fallback; invalid/contradictory responses use bounded content-failure fallback.

## Checkpoints and review

Candidates retain raw names, roles/counts, ordered language hints, and source fingerprints from normalized relations. Canonical display changes must not alter source identity.

`app/repositories/personality_checkpoints.py` composes PostgreSQL decisions/fingerprints/contracts/accepted identities/conflicts/explicit retries/model hints with local SQLite attempts/content exclusions/recovery evidence. Negatives create no canonical and never rewrite metadata. Unchanged current-contract decisions skip; changed inputs/prompt/schema or explicit persisted `retry_requested` reopen work without erasing evidence. No historical prompt/schema compatibility or CLI review/retry workbench exists.

Preserve `recovered_response` / `previous_failure` evidence; item HTTP 400 closes pending recoveries and records blocking evidence. Never reopen every failure automatically.

Reuse an exact-compatible active identity, preferring the checkpoint target; otherwise ambiguity requires review or a new unconfirmed identity is created. Preserve human approvals/reviewed associations. Multiple active alias targets require explicit resolution; normalization never resolves contributions.

Check review ownership before alias insertion. If its trigger creates a new owner-labeled placeholder, fill it with AI evidence and audit transactionally; preserve pre-existing human review.

## Queue and reporting

Untouched names precede checkpointed names before the limit. Each gets one first-pass turn and at most one later turn after that pass. Yield on 429, service 5xx, transport/local deadline failures after recording shared provider state, without content-excluding the model. Wait stoppably when capacity is unavailable; total daily exhaustion preserves untouched work. A second transient failure defers locally; stop preserves pending retries.

[Gemini runtime](gemini-runtime.md) owns personality pacing and I/O timeouts. Count unique people separately from physical `model_attempts`, distinguishing negatives, failures, final deferrals, and pending retries. Safe stop prevents new claims and finishes checkpoint boundaries; shared logging/progress/artifacts retain stopped/deferred outcomes.
