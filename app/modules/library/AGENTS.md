# Library flow guidance

These rules apply to `app/modules/library/`.

The PostgreSQL catalog is migrated, but backend adaptation is unfinished. Guidance below preserves workflow requirements; legacy table/field names in remaining code need verification against the catalog. See `docs/catalog-model.md` from the repo root. The former web review pages are removed.

Read only the guidance matching the files or behavior being changed:

Use `guidance/navigation.md` to locate implementation owners before reading code. It is a lookup table, not additional policy.

| Area | Guidance |
| --- | --- |
| source cache, previews, non-PDF conversion | `guidance/documents.md` |
| metadata extraction and evaluation | `guidance/metadata.md` |
| collection detection, validation, and apply | `guidance/collections.md` |
| static-site publishing export | `guidance/site-export.md` |

General Library rules:

- `~/.manzara/cache/source-documents` is a shared persistent, MD5-verified source cache, not a task artifact directory. Generated and temporary outputs stay in the retention-oriented `cache/` and `workspaces/` subtrees.
- Gemini workflows use only the shared configured Gemini model pool. Publisher merge analysis is the explicit exception: it uses configured Codex with ChatGPT subscription authentication, never provider or billing fallback. Read `guidance/publisher-merges.md` for this workflow.
- Flow work is resumable and must preserve per-item failure context and stable progress/artifact summaries.
- Library cleanup preparation is planning-only. Remote or catalog mutation requires the guarded Maintenance executor and explicit persisted review state.
