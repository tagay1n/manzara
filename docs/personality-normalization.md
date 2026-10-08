# Personality normalization contract

Owners: `app/modules/library/personality_normalization.py`, `personality_normalization_prompt.py`, `personality_workbench.py`, `runtime/run_normalize_personalities.py`, `app/repositories/normalization.py`, and `app/catalog/personality_normalization.py`. The CLI registers the flow-owned handler through `app/cli/`; only task controls and run inspection are enabled.

## Identity and outcomes

Initials accept one alphabetic character or Latin units `Kh`, `Sh`, `Ts`, `Ju`, normalized to an uppercase first letter and one trailing dot. Reject internal dots, multiple initials, and other multi-letter strings. Preserve full names/script; never expand initials.

The strict response requires every field:

- `normalized`: usable components, null reason; derive canonical name/key locally and atomically persist catalog identity/alias hypotheses, review evidence, audit records, and the PostgreSQL checkpoint. New AI associations remain unconfirmed.
- `not_person`: organization, website, or other non-person.
- `unusable`: potentially personal but unsafe ambiguous/corrupted input.

Negative decisions require a nonblank reason of at most 300 characters and null identity components/title/sex. Rare names or initials alone are not grounds for rejection. Valid negatives stop fallback; malformed/contradictory responses use bounded content-failure fallback.

## Checkpoints and review

Candidate reads use normalized publications/documents/names/credits and ordered publication languages, retaining raw names, roles/counts, language hints, and source fingerprints. Language hints now come from the migrated child relation; any resulting fingerprint change follows existing changed-input eligibility rules. Canonical display changes must not alter source identity. This adapted read does not establish readiness of every remaining normalization operation.

PostgreSQL checkpoints retain input fingerprints, contract versions, decisions, failure/recovery evidence, timestamps, and model hints. Negative decisions create no canonical and never rewrite source metadata. Unchanged negatives are skipped; changed inputs/contracts or explicit retry reopen eligible work.

Review operations remain backend services without a CLI workbench in this slice. Review retry requires the reviewed `updated_at`, rejects stale commands, and marks `retry_requested` without deleting evidence. Correct source metadata before retrying a negative decision.

Preserve the targeted legacy recovery rules implemented in code: compatible successes stay completed, unrelated terminal failures stay excluded, and only affected model exclusions are released. Keep `recovered_response` / `previous_failure` evidence. An item HTTP 400 closes all pending recoveries and records blocking evidence. Do not broaden recovery into an automatic retry of every failure.

A successful result reuses an exact-compatible active identity, preferring the checkpoint's existing target. Multiple compatible targets without that preference fail the item for explicit review. Otherwise it creates an unconfirmed identity. Existing human approvals and reviewed associations are preserved. A spelling with several active alias targets remains ambiguous in the operational review summary. Contributions are never resolved or rewritten by normalization.

Review ownership is checked before inserting the alias: the catalog trigger may create an owner-labeled placeholder during that insert. The transaction fills that new placeholder with AI evidence and audits the change; a pre-existing human review is preserved.

## Queue and reporting

Untouched names precede checkpointed names before applying the candidate limit. Workers share one queue; each name gets one first-pass turn and at most one later turn, after all first-pass workers finish.

Yield on 429, service 5xx, transport failures, and local deadlines without content-excluding the model. Record shared provider state first. Wait stoppably when no capacity is ready; total daily exhaustion preserves untouched work. A second transient failure leaves durable deferral; stopping preserves pending retry state.

Personality-only pacing and the 60-second HTTP I/O timeout are defined in [Gemini runtime](gemini-runtime.md). An I/O timeout is not a total streaming deadline.

Count unique people separately from physical `model_attempts`; distinguish negative outcomes, failures, final deferrals, and pending retries. Emit worker-attributed logs and explicit summary artifacts.

The CLI passes an explicit run context, shares its PostgreSQL pool, and propagates run logging into all worker threads. Stop prevents new claims and waits for safe checkpoint boundaries. Structured artifacts and final progress survive stopped/deferred runs; logs are never parsed into business results. The legacy normalization script now opens the same CLI with the task selected.
