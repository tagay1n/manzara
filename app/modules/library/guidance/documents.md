# Library document processing

## Previews

- Preview roles are selected with the pinned `yolov12l-doclaynet.pt` model at CPU `imgsz=1024`. Search the first and last three pages; treat page-header, page-footer, and picture layouts as non-relevant so pages containing only those classes are skipped. Keep roles distinct and persist the actual selected page numbers.
- Selection prefers the first useful front page, the last useful back page, then the next distinct useful front page. A valid PDF with no qualifying pages is complete with zero previews; fewer than three useful pages are also complete.
- Keep the shared source cache within `documents.cache_max_gib` (50 GiB by default). When it crosses the limit, evict least-recently-used completed sources to 90% of the limit; recent partial downloads and the source just materialized are protected.
- ISBN conflict review prefetches pending candidates sequentially into this same cache. Resolving a review or leaving the page never deletes its cached source; normal global cache eviction still applies.
- Object roles are deterministic and use compact S3 names. PostgreSQL stores document-level status, page count, recipe version, and the three nullable semantic page selections rather than a per-object manifest.
- Retain per-run and per-document preview workspaces under `~/.manzara`; never prune rendered previews automatically.

## Non-PDF extraction

- Extract every verified non-PDF source before language or Library classification. Reuse the shared cache and download misses only from primary Backblaze storage.
- Rich Markdown preserves tables, LaTeX, and monocorpus-style HTML figures. Referenced images use the configured public content-images bucket.
- QA cohorts are deterministic and capped per normalized catalog MIME type. Full-catalog promotion requires owner review.
- Before publishing, require a public HTML `<img>` reference for every prepared image. After a successful checkpoint, remove objects outside the document's expected key set.
- Retain converted documents, media, ASTs, Markdown, and archives under the run workspace. Publish only after verifying every new object and rechecking the source snapshot.
- All non-PDF sources use the shared final Pandoc formatter: LF newlines, canonical headings/lists/fences, no prose wrapping, and one terminal newline. Preserve Unicode punctuation, literal plain-text syntax, fenced code contents, LaTeX, and raw HTML figures/tables. Retain both `unformatted.md` and normalized `final.md`; the local `<md5>.zip` contains exactly that final Markdown.
- Retained extraction files live under `~/.manzara/workspaces/library/non-pdf-extraction/run-<run-id>/<md5>/` (or the configured artifacts root). The task never deletes them after publication. Persist a compact `library.non_pdf_local_content` artifact event with Markdown/archive/validation paths before remote publication, and include `workspace_path` in the run summary. Downstream metadata workspaces are separate and may be removed without affecting extraction outputs.
- Convert legacy DOC/RTF locally first, normalize LibreOffice DOCX ZIP metadata before parsing, and use the established Google Drive import/export path only when LibreOffice fails or produces an invalid DOCX. Always delete the temporary Drive file. Treat `~$` Word owner files as invalid temporary sources. Converter failures are operational outcomes, not proof that the source document is corrupt.
- Queue a guarded `corrupted` move only when verified non-PDF source bytes fail deterministic container, decoding, or input-parser checks. Converter timeouts, unsupported formats, OCR-only content, output validation, and storage failures remain non-corruption outcomes.
- PPTX extraction reads visible slides in presentation order and preserves native text, lists, and tables. Hidden slides and speaker notes are excluded. Shape order is top-to-bottom, then left-to-right using explicit coordinates (including group transforms); missing positions fall back to stable package shape order.
- Defer the entire PPTX before publication when a visible slide contains a picture, picture fill/background, chart, SmartArt, embedded object, equation, or unsupported graphic payload. Layout/master images and unreferenced media do not block extraction. Native-text-empty decks are also deferred; no OCR or generated descriptions are used.
- PPTX findings live in each document workspace's `pptx-inspection.json`, linked by a compact persisted `task.artifact` event. The run summary includes inspected/extracted/image/unsupported-visual/empty deck counts; visual findings overlap. Oversized XML is deferred with an incomplete inspection report, excluded from the completed-inspection count. Deferred outcomes preserve existing content and require an explicit retry or a new recipe version.
- `nonpdf.v8` adds PPTX support. Recipe changes make older records eligible for reprocessing across supported formats; use a capped QA cohort and review before a full-catalog run.
- `nonpdf.v9` applies one final Markdown formatting policy to every supported format, including Markdown and plain text, and exposes retained local output paths. Previously published objects require reprocessing to acquire the new formatting.
