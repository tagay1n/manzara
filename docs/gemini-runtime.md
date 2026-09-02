# Shared Gemini runtime contract

Read this only when changing `app/gemini_*.py` or a Gemini-consuming workflow.

- All Gemini calls use the shared runtime manager. Resolve model aliases and pools from config; task logic must not hardcode model names.
- Keys are grouped by account and runtime state is PostgreSQL-backed per `(account, key, model)`. Choose a random account, then a random usable key, and try alternatives before waiting. Permit at most one request per minute per tuple.
- Daily exhaustion is model-scoped and clears at reset rollover. Block new requests from one hour before through one hour after Pacific reset; owner overrides apply only to the active window and emit an audit event.
- A `429` fails by default; declared model-pool workflows may persist the attempt and advance. A `400` rejects only the item. A `5xx` starts the shared 60-second pause.
- Transport and `5xx` failures share a bounded retry budget. Authentication/configuration errors are task-fatal. Uploaded Gemini files use shared best-effort cleanup.
- Parallel workflows emit every physical stdout line through the shared worker logger using `[worker=<flow>-<one-based-id>]`; pool-level messages use `worker=coordinator`. Keep multiline responses attributed line by line so the task viewer can color and group them without parsing business content.
