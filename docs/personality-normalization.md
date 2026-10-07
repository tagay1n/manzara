# Personality normalization contract

Owners: `app/modules/library/personality_normalization.py`, `personality_normalization_prompt.py`, `personality_workbench.py`, `runtime/run_normalize_personalities.py`, and `app/repositories/normalization.py`.

## Identity and outcomes

Initials accept one alphabetic character or Latin units `Kh`, `Sh`, `Ts`, `Ju`, normalized to an uppercase first letter and one trailing dot. Reject internal dots, multiple initials, and other multi-letter strings. Preserve full names/script; never expand initials.

The strict response requires every field:

- `normalized`: usable components, null reason; derive canonical name/key locally and persist canonical plus source alias atomically.
- `not_person`: organization, website, or other non-person.
- `unusable`: potentially personal but unsafe ambiguous/corrupted input.

Negative decisions require a nonblank reason of at most 300 characters and null identity components/title/sex. Rare names or initials alone are not grounds for rejection. Valid negatives stop fallback; malformed/contradictory responses use bounded content-failure fallback.

## Checkpoints and review

Candidate reads use normalized publications/documents/names/credits, retaining raw names, roles/counts, language hints, and source fingerprints. Canonical display changes must not alter source identity. This adapted read does not establish readiness of every remaining normalization operation.

PostgreSQL checkpoints retain input fingerprints, contract versions, decisions, failure/recovery evidence, timestamps, and model hints. Negative decisions create no canonical and never rewrite source metadata. Unchanged negatives are skipped; changed inputs/contracts or explicit retry reopen eligible work.

Review retry requires the reviewed `updated_at`, rejects stale commands, and marks `retry_requested` without deleting evidence. Correct source metadata before retrying a negative decision.

Preserve the targeted legacy recovery rules implemented in code: compatible successes stay completed, unrelated terminal failures stay excluded, and only affected model exclusions are released. Keep `recovered_response` / `previous_failure` evidence. An item HTTP 400 closes all pending recoveries and records blocking evidence. Do not broaden recovery into an automatic retry of every failure.

## Queue and reporting

Untouched names precede checkpointed names before applying the candidate limit. Workers share one queue; each name gets one first-pass turn and at most one later turn, after all first-pass workers finish.

Yield on 429, service 5xx, transport failures, and local deadlines without content-excluding the model. Record shared provider state first. Wait stoppably when no capacity is ready; total daily exhaustion preserves untouched work. A second transient failure leaves durable deferral; stopping preserves pending retry state.

Personality-only pacing and the 60-second HTTP I/O timeout are defined in [Gemini runtime](gemini-runtime.md). An I/O timeout is not a total streaming deadline.

Count unique people separately from physical `model_attempts`; distinguish negative outcomes, failures, final deferrals, and pending retries. Emit worker-attributed logs and explicit summary artifacts.
