# Library flow rules

Applies to `app/modules/library/`. Use [owner lookup](guidance/navigation.md), then only matching guidance:

| Area | Contract |
| --- | --- |
| Sources, previews, conversion | [Documents](guidance/documents.md) |
| Metadata extraction/evaluation | [Metadata](guidance/metadata.md) |
| Collection proposals | [Collections](guidance/collections.md) |
| Publisher proposals / Codex exception | [Publishers](guidance/publisher-merges.md) |
| Static publishing | [Export](guidance/site-export.md) |

- Use the configured MD5-verified persistent source cache; generated/temporary outputs belong in artifact `cache/` or `workspaces/` subtrees.
- Gemini consumers use the shared configured pool. Publisher analysis uses configured subscription-authenticated Codex without provider/billing fallback.
- Preserve resumability, item failure context, and stable progress/artifact summaries.
- Cleanup preparation is planning-only. Remote/catalog cleanup requires the guarded Maintenance executor and persisted review state.
