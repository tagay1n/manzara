# Personality normalization contract

Initial fields accept one alphabetic character or the explicit Latin
transliteration units `Kh`, `Sh`, `Ts`, and `Ju`. Case is normalized to an uppercase
first letter and lowercase remaining letters, with one trailing dot. The input
may omit that dot. Internal dots, multiple initials and other multi-letter strings
are rejected. Full names and script are preserved; initials are never expanded.

The Gemini prompt includes exactly five complementary examples: male and female
Tatar patronymics, Arabic-script conversion, compact Cyrillic initials and
historical Latin spelling. Nine other examples remain disabled. All examples use
the outcome contract, including the disabled institution example. For `Example
Name` with no language hints, the initial five-example prompt was 3,491 characters
versus 4,218 before reduction. The completed outcome prompt is 3,922 characters,
including its added outcome rules and fields, excluding the separate response
schema. These are character measurements, not token counts, and do not establish
that prompt length caused upstream quota or service errors.

## Outcomes and durable checkpoints

Gemini requests and local validation use `PersonalityResponse`, prompt
`personality-outcomes-v9` and schema `person-outcomes-v2`. Every field is required:

- `normalized` requires usable name components and a null reason. The application
  derives the canonical name and identity key locally and saves the canonical and
  source alias atomically.
- `not_person` identifies an organization, website or other non-person entity.
- `unusable` identifies potentially personal but unsafe ambiguous/corrupted input.

Negative decisions require a nonblank reason of at most 300 characters and null
identity components, title and sex. Unfamiliar names, rare spellings and initials
alone are not grounds for an unusable decision. Valid negative decisions stop
model fallback; malformed JSON or contradictory components still use bounded
content-failure fallback.

No database migration is required. PostgreSQL checkpoints retain the exact raw
name, source fingerprint, source roles/counts and contract versions. Negative
states are `not_person` and `unusable`, with their reason in `failure_context`,
completion time, and model decision/language hints in `attempted_models`. They
create no canonical record and never rewrite source metadata. Unchanged negative
decisions are skipped on reruns; relevant input or contract changes reopen them.

Compatible v8 successes remain completed. The existing v7 preprocessing
compatibility rule remains in effect. Unchanged unrelated terminal failures stay
excluded. Legacy all-null failures under `person-components-v1` and failures from
the former single-letter initials rule receive targeted recovery. Only affected
model exclusions are released. Original evidence is kept as `recovered_response`
and under `previous_failure` if the recovered model fails again. Current contract
failures do not qualify repeatedly for legacy all-null recovery.

An item-level HTTP 400 rejection closes all pending recoveries for that person,
including models not reached. Original errors remain under `recovery_blocked`,
with the rejection recorded in `recovery_blocked_by`.

The personalities workbench shows separate counts and a PostgreSQL-backed,
40-row paginated review of Not a person, Needs review, Failed, Deferred and Retry
requested. Correct the source metadata before using Retry after correction on a
negative decision. The retry endpoint requires the reviewed `updated_at` value,
rejects stale requests, and changes the checkpoint to `retry_requested` without
deleting its evidence. The next normalization run retries it. Explicit retry and
changed inputs release old content exclusions; automatic scheduling does not
reopen unrelated terminal failures.

## Shared queue and retry bounds

Untouched names precede names carrying checkpoints, before the candidate limit.
Workers claim from one shared queue. Each eligible raw name gets one first-pass
turn and at most one later turn. The retry phase starts only after the initial
queue and all first-pass workers have finished, including stop-aware waits.

Personality normalization opts into yielding immediately on HTTP 429, service
5xx (including 503 and 504), transport errors and local request deadlines. These
errors preserve retryable checkpoints and move the person to the tail without
permanently excluding the model. HTTP 504 remains a service error with the shared
60-second model pause; a local deadline is a transient timeout, not HTTP 504.
Service pauses finish before another turn from that worker. Quota cooldowns,
provider retry metadata, daily exhaustion, per-key limits and the global request
limiter remain enforced by the shared runtime. No escalating overload pause is
introduced. Extraction and evaluation retain their existing fallback behavior.

If all models are cooling down with a known retry time, wait until the earliest
shared retry time before continuing. If the pool has no usable keys or known
retry time, stop processing more names and preserve untouched work for resumption.
A second transient failure leaves the person durably deferred. Stop during a
pause preserves the saved deferral and pending retry for a later run.

Progress and summaries count unique people, with `not_person`, `unusable` and
`retry_pending` separate from successes, failures and final deferrals. Physical
requests remain in `model_attempts`; moving a person to the tail does not count
as another processed person. Queue moves, decisions, reasons and waits are
worker-attributed in the dedicated task log. Final counters are explicit structured
artifacts, never inferred from log parsing.
