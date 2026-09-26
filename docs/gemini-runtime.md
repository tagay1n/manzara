# Shared Gemini runtime contract

Read this only when changing `app/gemini_*.py` or a Gemini-consuming workflow.

- All Gemini calls use the shared runtime manager. Resolve model aliases and pools from config; task logic must not hardcode model names.
- Keys are grouped by owner account, while quota state is grouped by configured Google project/domain and model. A plain string key defaults to its own independent quota domain; use `{api_key, quota_domain}` entries to give keys from the same project a shared domain. Choose a random account, then a random usable key, and try alternative quota domains before deferring. Permit at most one request per minute per key/model tuple. Concurrent task processes coordinate through short SQLite transactions in WAL mode.
- Apply the `gemini.runtime` limits to every task. The defaults reserve at most ten physical requests in one shared rolling minute and try at most three quota domains for one model/item. Request reservations and circuit signals are disposable local SQLite state, shared across task processes.
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
pass, with stop-aware waits and durable deferral. Other workflows retain the
default ordered fallback and bounded same-item retry policy.
