# Shared Gemini runtime contract

Read this only when changing `app/gemini_*.py` or a Gemini-consuming workflow.

- All Gemini calls use the shared runtime manager. Resolve model aliases and pools from config; task logic must not hardcode model names.
- Keys are grouped by owner account, while quota state is grouped by configured Google project/domain and model. A plain string key defaults to its own independent quota domain; use `{api_key, quota_domain}` entries to give keys from the same project a shared domain. Choose a random account, then a random usable key, and try alternative quota domains before deferring. Permit at most one request per minute per key/model tuple. Concurrent task processes coordinate through short SQLite transactions in WAL mode.
- Only a `429` with explicit per-day quota evidence causes quota-domain/model exhaustion until Pacific reset. Generic, RPM, TPM, rolling-spend, and shared-capacity `429` responses start a persisted quota-domain/model cooldown instead. Honor Gemini retry metadata and otherwise use bounded exponential cooldowns from one to ten minutes. A successful request clears that cooldown history.
- Daily exhaustion clears at reset rollover. Block new requests from one hour before through one hour after Pacific reset; owner overrides apply only to the active window and emit an audit event.
- A `400` rejects only the item. A `5xx` starts the shared 60-second model pause.
- Transport and `5xx` failures share a bounded retry budget. Authentication/configuration errors are task-fatal. Uploaded Gemini files use shared best-effort cleanup.
- Parallel workflows emit every physical stdout line through the shared worker logger using `[worker=<flow>-<one-based-id>]`; pool-level messages use `worker=coordinator`. Keep multiline responses attributed line by line so the task viewer can color and group them without parsing business content.
