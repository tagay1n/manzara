# Library document processing

These are workflow requirements; catalog-dependent repositories still need compatibility verification. Owners: [navigation](navigation.md).

## Sources and previews

- Use the shared persistent MD5-verified source cache. Populate processing misses from verified primary Backblaze storage. Enforce `documents.cache_max_gib` (default 50 GiB); evict least-recently-used completed sources to 90% when exceeded, protecting recent partial downloads and the current source.
- Preview detection uses pinned `yolov12l-doclaynet.pt`, CPU `imgsz=1024`, first/last three pages. Ignore page-header/footer/picture-only layouts. Select first useful front, last useful back, then next distinct front; persist actual page numbers and distinct roles. Zero/fewer than three qualifying pages is a completed outcome.
- Legacy preview checkpoints retain status, page count, recipe, and nullable selected roles. New catalog requests use durable generation/lease records. Retain rendered workspaces; never prune previews automatically. Review-prefetched sources remain cached until ordinary eviction.

## Extraction and publication

- Extract verified non-PDF sources before language/classification decisions. Treat MIME as a hint; inspect byte signatures. OLE root streams distinguish DOC/PPT/XLS; nested objects do not determine outer format. Ambiguous/unreadable OLE remains unsupported. Persist verified DOC/PPT MIME against the unchanged source; XLS has no catalog correction.
- Preserve tables, LaTeX, and HTML figures in rich Markdown. Apply the shared Pandoc final formatter: LF, canonical headings/lists/fences, no prose wrapping, one terminal newline; preserve Unicode punctuation, literal text/code/LaTeX/HTML.
- Retain `unformatted.md`, `final.md`, converted sources/media/ASTs, validation reports, and an archive containing exactly final Markdown under `workspaces/library/non-pdf-extraction/run-<run-id>/<md5>/`. Emit a compact persisted `library.non_pdf_local_content` artifact before remote publication; include workspace paths in the summary.
- Every prepared image needs a public HTML reference. Publish only after object verification and source-snapshot recheck; then remove objects outside the expected key set. Keep downstream metadata workspaces separate.
- QA cohorts are deterministic and capped per normalized MIME. Full-catalog promotion and known-source repairs need owner review. Use repeated `--only-md5` plus `--retry-known-failures` for exact reviewed retry cohorts; preserve prior outputs for comparison.
- Only deterministic verified-byte container/decoding/parser failures justify a guarded `corrupted` move. Unsupported/OCR-only content, converter timeouts, output-validation/storage failures, and missing tools are operational or deferred outcomes, never proof of corruption.
- Recipe versions live in `non_pdf_types.py`; inspect code instead of maintaining a second version ledger here.

## Converter contracts

| Format | Required behavior |
| --- | --- |
| DOC/RTF | LibreOffice first; normalize converted DOCX ZIP metadata. Google Drive fallback only for failed/invalid conversion; always delete temporary Drive files. Word owner files are invalid sources. |
| Large DOCX | Stream paragraphs/images to HTML only when body XML is oversized and has no tables; otherwise retain normal reader. Omit empty image files with workspace evidence. |
| PPTX | Visible slides only, coordinate ordering with stable fallback; preserve native text/lists/tables. Defer whole deck for visible pictures/fills/backgrounds, charts, SmartArt, embedded objects/equations, unsupported visuals, or no native text. Ignore hidden slides/notes and unreferenced/master media. No OCR/generated descriptions. |
| Legacy PPT | Stage byte-detected `.ppt`, convert to PPTX locally; Google Slides fallback for failed/invalid/text-empty conversion, always cleaning remote temporary files. Preserve slide boundaries; ambiguous drawing relations defer. |
| XLS/XLSX | Stage correct suffix, LibreOffice HTML; retain visible sheet headings/tables, exclude navigation/images/charts/formula expressions. Displayed values may be calculated without source caches. |
| ODT | Verify ZIP, LibreOffice HTML; retain prose/headings/tables/sidecar images, then shared formatter. |
| MOBI | Verify BOOKMOBI, Calibre `ebook-convert` to EPUB, Pandoc parsing; retain MOBI source identity. No Google fallback. |
| HTML | Decode declared charset with verified legacy fallback; flatten prose layout tables, preserve data tables, retain normalized UTF-8 source. |

PPTX inspection persists `pptx-inspection.json` and a compact artifact. Summary counts distinguish inspected/extracted/image/unsupported/empty decks; visual counts overlap. Oversized XML produces incomplete inspection and no completed-inspection count. Deferred outcomes retain existing content and reopen only on explicit retry/new recipe.
