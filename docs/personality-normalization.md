# Personality normalization contract

Initial fields accept one alphabetic character or the explicit Latin
transliteration units `Kh`, `Sh`, `Ts`, and `Ju`. These catalog spellings represent
single initials. Case is normalized to an uppercase first letter and lowercase
remaining letters, with exactly one trailing dot. The input may omit that dot.
Internal dots, multiple initials, and other multi-letter strings are rejected.
Full names and script are preserved; initials are never expanded locally.

The Gemini prompt includes five complementary examples: male and female Tatar
patronymics, Arabic-script conversion, compact Cyrillic initials, and historical
Latin spelling. The other nine examples are retained in the disabled collection
and are not sent to Gemini. For `Example Name` with no language hints, the revised
prompt is 3,491 characters versus 4,218 before the change (excluding the separate
response schema). This measures characters, not tokens, and does not establish
that prompt length caused the observed quota or service errors.

A strict `PersonalityResponse` contract is prepared for `normalized`, `not_person`,
and `unusable` decisions. Every field is required. Negative decisions require a
nonblank reason and null identity components; normalized decisions require usable
components and a null reason. This contract is not yet connected to Gemini calls
or persistence. Durable outcome/versioning policy and bounded queue retries await
owner confirmation as required by `PERSONALITY_NORMALIZATION_HANDOFF.md`.
Candidate selection now uses a stable partition: untouched names precede names
with durable checkpoints, before the limit is applied. Parallel workers claim
names from one shared queue, rather than private partitions. This preserves
priority across workers and restarts. Same-run transient tail retries are not yet
implemented; existing shared Gemini fallback and cooldown behavior remains.

Terminal failures caused by the former single-letter validation rule are eligible
for a targeted retry when the raw name or saved error contains a recognized
transliterated initial. Only those model exclusions are released. The old failure
is retained as `recovered_response` evidence, and preserved under
`previous_failure` if that model fails again. Unrelated model exclusions,
malformed initials, and completed successful checkpoints remain unchanged. A new
failure uses the current validator message and does not qualify for this recovery
again.

An item-level HTTP 400 rejection closes every pending recovery for that person,
including models not reached before the rejection. Their original errors remain
stored with `kind: recovery_blocked` and the rejection in `recovery_blocked_by`.
This prevents a new terminal rejection from reopening on every restart.
