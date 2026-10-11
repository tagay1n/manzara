# Shared Gemini runtime

Read when changing `app/gemini_*.py` or consumers. All calls use the shared runtime/configured model pool; never hardcode task models. Configuration uses account-to-key-list mappings, with strings or `{api_key, quota_domain}` entries; retired aliases/account lists fail.

## Scheduling and leases

Keys belong to owner accounts; quotas belong to configured Google project/domain + model. Plain keys default to independent quota domains; explicitly group keys sharing a project. Atomic SQLite claims and round-robin cursor select ready models across processes, skipping pauses/exhaustion/item exclusions. Prefer idle accounts, then least-recently-used eligible projects; independent projects under busy accounts remain usable.

One renewable owner-token lease per project spans all models; heartbeat/release are guarded and expired leases recover crashes. Configured project/model spacing advances at actual generation start after preparation/uploads under the lease. Each task sends one request at a time; lease support threads are allowed.

Provider-led quota control uses spacing and responses, without billing ledgers, proactive quota tables, token-count calls, or a global ten-request/minute cap. Required `gemini.runtime` policies configure quota rotations, transient budgets, spacing/leases, waits, cooldowns, model pauses, and generic circuits. State is disposable SQLite.

| Outcome | Shared action |
| --- | --- |
| 429 with explicit per-day evidence | Exhaust domain/model until manual `/reset` |
| Generic/RPM/TPM/rolling-spend/shared-capacity 429 | Persist cooldown; honor retry metadata or bounded exponential policy |
| Distinct-domain generic 429 threshold/window | Pause model for configured circuit duration |
| Success | Clear model generic-429 history and successful-domain cooldown history |
| 400 | Reject only the item |
| 5xx | Configured model pause; share bounded transient budget with transport failures |
| Authentication/configuration error | Fail task |

Exhaustion survives Pacific quota-day rollover and current-schema restarts. Rollover resets only daily attempt/success counters; requests have no midnight blackout. Temporary cooldowns and model pauses retain their normal expiry. Uploaded files receive best-effort cleanup. Shared redacted stdout logging and local provider-wait snapshots follow root/task-runtime rules.

`/gemini` reads local state for the current YAML keys/model pool and prints only configured key/account totals and exhausted-key counts per model. Missing state records contribute no recorded exhaustion; the report does not initialize provider state or query remaining Google quota. `/reset` atomically clears exhaustion flags/timestamps for those keys/models and reports the number of key/model states cleared. It preserves cooldowns, pauses, leases, spacing, counters, and error history. Both commands work during a foreground run; later provider failures can exhaust keys again. Reset does not restart a finished/deferred task; use `/run` to resume it.

## Personality queue and pacing

Personality normalization uses `run_ordered_model_pool(..., yield_on_transient=True)`: 429/5xx/transport/deadlines yield immediately after provider state is recorded, without content exclusions. Its queue owns one later turn; item-only exclusions yield while other names run, and total daily exhaustion leaves untouched work. Other consumers retain bounded same-item retry policies.

Only personality uses run-scoped `GeminiPacingPolicy` from `gemini.personality_pacing`. Increasing interval/cooldown sequences and quota/success thresholds govern slowdown, exclusive probes, escalation, and recovery. Response time counts toward spacing. Service failures preserve recovery progress plus shared pauses; quota/transport/timeout/neutral outcomes reset progress. Failed service probes escalate cooldowns. Content-validation failures count as provider availability without changing item semantics.

Renewable SQLite leases own admission/probes; preparation reserves admission and transport advances generation spacing. Epochs prevent pre-cooldown responses from reopening the queue. Logs/wait snapshots report transitions. Pacing persists within a run; new runs start at the configured initial interval while shared provider cooldowns remain. Stop gracefully before coordination changes; incompatible lease implementations must not overlap.

`gemini.request` owns request/personality/metadata I/O timeouts, file activation/polling, sampling, and response-log limits. I/O timeout is not a hard total streaming deadline. Metadata extraction uses its task-scoped `metadata.response_timeout_seconds`, and evaluation uses independent `metadata.evaluation_response_timeout_seconds`, for generation I/O and the total response deadline, editable in `/settings` from 1 to 900 seconds. Async Gemini transport cancels generation and the complete stream at that deadline; uploads, activation, provider admission/spacing, and cleanup remain outside it. Both metadata tasks yield on response timeouts as retryable operational failures without consuming content attempts; other consumers retain their existing timeouts/retry policies.
