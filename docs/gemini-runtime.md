# Shared Gemini runtime contract

Read this only when changing `app/gemini_*.py` or a Gemini-consuming workflow.

- All Gemini calls use the shared runtime manager. Resolve model aliases and pools from config; task logic must not hardcode model names.
- Keys are grouped by owner account; quota state is grouped by configured Google project/domain and model. A plain string key defaults to an independent quota domain; use `{api_key, quota_domain}` entries for keys sharing a project. All workflows use a shared round-robin cursor to select the next ready configured model, skipping pauses, exhausted projects and item content exclusions. Claims and cursor advances are atomic SQLite transactions shared across threads and processes.
- Prefer accounts without active requests, then the least recently used eligible project. Independent projects under a busy account remain usable. Hold one renewable request lease per project across all models; owner tokens protect heartbeat/release, and expired leases recover after a crashed worker. Allow at most one generation start per minute per project/model, including when multiple keys share a project. Shared request transports advance spacing at the actual generation start after uploads/preparation, under the same lease. Preserve configured worker counts.
- Quota control is provider-led: conservative project/model spacing plus Gemini's quota responses, without a billing ledger, proactive quota table or token-count API calls. There is no global ten-request/minute cap. `gemini.runtime` still bounds per-item quota rotations (default three per model) and generic-quota circuits. Cooldowns and daily exhaustion remain disposable local SQLite state.
- Only a `429` with explicit per-day quota evidence causes quota-domain/model exhaustion until Pacific reset. Generic, RPM, TPM, rolling-spend, and shared-capacity `429` responses start a persisted quota-domain/model cooldown instead. Honor Gemini retry metadata and otherwise use bounded exponential cooldowns from one to ten minutes. Generic `429` responses from three distinct quota domains within 60 seconds open the shared model circuit for 60 seconds. A successful request clears that model's generic-429 circuit history and the successful domain's cooldown history.
- Daily exhaustion clears at reset rollover. Block new requests from one hour before through one hour after Pacific reset; owner overrides apply only to the active window and emit an audit event.
- A `400` rejects only the item. A `5xx` starts the shared 60-second model pause.
- Transport and `5xx` failures share a bounded retry budget. Authentication/configuration errors are task-fatal. Uploaded Gemini files use shared best-effort cleanup.
- Parallel workflows emit every physical stdout line through the shared worker logger using `[worker=<flow>-<one-based-id>]`; pool-level messages use `worker=coordinator`. Keep multiline responses attributed line by line so the task viewer can color and group them without parsing business content.

Personality normalization opts into `run_ordered_model_pool(...,
yield_on_transient=True)`: it yields the item immediately on 429, service 5xx,
transport failures and local deadlines. The shared runtime first records its
normal key/quota/model state. These transient outcomes do not exclude a model as
a content failure. The personality queue handles one later turn after the first
pass, with durable deferral. A worker immediately proceeds to the next person
using another ready model rather than sleeping for the failed model. The
scheduler waits stoppably for earliest availability only when the entire pool
has no ready capacity. An item blocked by its content exclusions yields while
other items can run; total daily exhaustion ends the queue without consuming
untouched work. Other workflows retain bounded same-item retry and checkpoint
policies while using the same ready-model scheduler.

Local schema version 4 adds the cursor, project leases and project/model spacing
without changing PostgreSQL checkpoints or clearing quota evidence. Existing
key cooldowns also remain eligibility barriers during the transition. Stop all
Gemini workers gracefully before upgrading, then resume from their checkpoints;
old account-lease workers must not overlap new project-lease workers.
