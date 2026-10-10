# Shared Gemini runtime contract

Read this only when changing `app/gemini_*.py` or a Gemini-consuming workflow.

- All Gemini calls use the shared runtime manager. Resolve model aliases and pools from config; task logic must not hardcode model names.
- Keys are grouped by owner account; quota state is grouped by configured Google project/domain and model. A plain string key defaults to an independent quota domain; use `{api_key, quota_domain}` entries for keys sharing a project. All workflows use a shared round-robin cursor to select the next ready configured model, skipping pauses, exhausted projects and item content exclusions. Claims and cursor advances are atomic SQLite transactions shared across threads and processes.
- Prefer accounts without active requests, then the least recently used eligible project. Independent projects under a busy account remain usable. Hold one renewable request lease per project across all models; owner tokens protect heartbeat/release, and expired leases recover after a crashed worker. Allow at most one generation start per minute per project/model, including when multiple keys share a project. Shared request transports advance spacing at the actual generation start after uploads/preparation, under the same lease. Preserve configured worker counts.
- Quota control is provider-led: conservative project/model spacing plus Gemini's quota responses, without a billing ledger, proactive quota table or token-count API calls. There is no global ten-request/minute cap. `gemini.runtime` still bounds per-item quota rotations (default three per model) and generic-quota circuits. Cooldowns and daily exhaustion remain disposable local SQLite state.
- Only a `429` with explicit per-day quota evidence causes quota-domain/model exhaustion until Pacific reset. Generic, RPM, TPM, rolling-spend, and shared-capacity `429` responses start a persisted quota-domain/model cooldown instead. Honor Gemini retry metadata and otherwise use bounded exponential cooldowns from one to ten minutes. Generic `429` responses from three distinct quota domains within 60 seconds open the shared model circuit for 60 seconds. A successful request clears that model's generic-429 circuit history and the successful domain's cooldown history.
- Daily exhaustion clears at reset rollover. Block new requests from one hour before through one hour after Pacific reset. The CLI exposes no quota reset or blackout override commands.
- A `400` rejects only the item. A `5xx` starts the shared 60-second model pause.
- Transport and `5xx` failures share a bounded retry budget. Authentication/configuration errors are task-fatal. Uploaded Gemini files use shared best-effort cleanup.
- Parallel workflows attribute every physical stdout line using `[worker=<flow>-<one-based-id>]`; pool-level messages use `worker=coordinator`. Keep multiline responses attributed without parsing business content.

Personality normalization opts into `run_ordered_model_pool(...,
yield_on_transient=True)`: it yields the item immediately on 429, service 5xx,
transport failures and local deadlines. The shared runtime first records its
normal key/quota/model state. These transient outcomes do not exclude a model as
a content failure. The personality queue handles one later turn after the first
pass, with durable deferral. A worker proceeds to the next person using another
ready model, subject to the personalities-only pacing gate below. The scheduler
waits stoppably when the pool has no ready capacity or the run pacing gate is
closed. An item blocked by its content exclusions yields while
other items can run; total daily exhaustion ends the queue without consuming
untouched work. Other workflows retain bounded same-item retry and checkpoint
policies while using the same ready-model scheduler.

Personality normalization also opts into a run-scoped `GeminiPacingPolicy`.
All its workers and models share five-second spacing between generation starts;
response time counts toward that interval. Other tasks do not enable this gate.
Each generic 429 increases spacing through 5, 10, 20, 40 and 60 seconds. Three
quota failures without a successful response pause the queue for 60 seconds.
The next eligible request is an exclusive probe; other workers wait for its
outcome. A generic 429, service failure, transport failure or timeout during a
probe escalates the cooldown through 120, 240, 480 and 600 seconds (capped).
A successful probe resumes at the slowed interval, with a ten-second minimum.
Five successful responses reduce spacing by one level. Ordinary model service
5xx failures preserve that progress while retaining the shared model pause;
quota, transport, timeout and neutral outcomes reset it. Failed service probes
still escalate cooldowns. Reaching five seconds resets cooldown escalation.
A generic 429 during recovery pauses
the queue immediately. Content validation failures count as provider availability
for pacing without changing their item failure semantics. Daily quota exhaustion
and ordinary service/transport failures retain their shared runtime handling.
Personality normalization sets the shared transport's HTTP I/O timeout to 60
seconds; other callers retain their existing timeout. This is an I/O timeout,
not a hard deadline for the total duration of a streamed response.

Admission and probe ownership use renewable SQLite leases. Preparation reserves
admission; the transport advances spacing at the actual generation start.
Epochs prevent responses from before a cooldown from reopening the queue.
`gemini.pacing.changed` events and worker log lines describe interval, cooldown
and probe transitions. Workers recovering within the same
run retain pacing state; each new run starts fresh at five seconds while existing
shared provider cooldowns still apply. Stop workers gracefully before changing
coordination contracts; incompatible lease implementations must not overlap.
