# Library document processing

## Previews

- Preview roles are selected with the pinned `yolov12l-doclaynet.pt` model at CPU `imgsz=1024`. Search the first and last three pages; treat page-header, page-footer, and picture layouts as non-relevant so pages containing only those classes are skipped. Keep roles distinct and persist the actual selected page numbers.
- Selection prefers the first useful front page, the last useful back page, then the next distinct useful front page. A valid PDF with no qualifying pages is complete with zero previews; fewer than three useful pages are also complete.
- Keep the shared source cache within `documents.cache_max_gib` (50 GiB by default). When it crosses the limit, evict least-recently-used completed sources to 90% of the limit; recent partial downloads and the source just materialized are protected.
- Object roles are deterministic and use compact S3 names. PostgreSQL stores document-level status, page count, recipe version, and the three nullable semantic page selections rather than a per-object manifest.
- Retain per-run and per-document preview workspaces under `~/.manzara`; never prune rendered previews automatically.

## Non-PDF extraction

- Extract every verified non-PDF source before language or Library classification. Reuse the shared cache and download misses only from primary Backblaze storage.
- Rich Markdown preserves tables, LaTeX, and monocorpus-style HTML figures. Referenced images use the configured public content-images bucket.
- QA cohorts are deterministic and capped per normalized catalog MIME type. Full-catalog promotion requires owner review.
- Before publishing, require a public HTML `<img>` reference for every prepared image. After a successful checkpoint, remove objects outside the document's expected key set.
- Retain converted documents, media, ASTs, Markdown, and archives under the run workspace. Replace legacy public content only after verifying every new object and rechecking the source snapshot.
- Convert legacy DOC/RTF locally first, normalize LibreOffice DOCX ZIP metadata before parsing, and use the established Google Drive import/export path only when LibreOffice fails or produces an invalid DOCX. Always delete the temporary Drive file. Treat `~$` Word owner files as invalid temporary sources. Converter failures are operational outcomes, not proof that the source document is corrupt.
- Queue a guarded `corrupted` move only when verified non-PDF source bytes fail deterministic container, decoding, or input-parser checks. Converter timeouts, unsupported formats, OCR-only content, output validation, and storage failures remain non-corruption outcomes.
