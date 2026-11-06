# Library flow rules

Applies to `app/modules/library/`. Start with the matching row in [architecture](../../../docs/architecture.md), then read only the relevant contract sections. Shared Library rules below apply to every change. The [metadata contract](guidance/metadata.md) separates discovery, persistence, retries, extraction, and evaluation; do not preload unrelated sections.

- Use the configured MD5-verified persistent source cache; generated/temporary outputs belong in artifact `cache/` or `workspaces/` subtrees.
- Gemini consumers use the shared configured pool. Publisher analysis uses configured subscription-authenticated Codex without provider/billing fallback.
- Preserve resumability, item failure context, and stable progress/artifact summaries.
- Cleanup preparation is planning-only. Remote/catalog cleanup requires the guarded Maintenance executor and persisted review state.
