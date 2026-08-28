# Library flow guidance

These rules apply to `app/modules/library/`.

Read only the guidance matching the files or behavior being changed:

| Area | Guidance |
| --- | --- |
| source cache, previews, non-PDF conversion | `guidance/documents.md` |
| metadata extraction and evaluation | `guidance/metadata.md` |
| collection detection, validation, and apply | `guidance/collections.md` |

General Library rules:

- `~/.monocorpus/0_entry_point` is a shared persistent, MD5-verified source cache, not a task artifact directory. Generated and temporary outputs stay under `~/.manzara`.
- Models come only from the shared configured Gemini model pool.
- Flow work is resumable and must preserve per-item failure context and stable progress/artifact summaries.
- Library cleanup preparation is planning-only. Remote or catalog mutation requires the guarded Maintenance executor and explicit persisted review state.
