# Library document processing

Non-PDF extraction has a catalog-native CLI handler. Other catalog-dependent document/preview workers still need adaptation and verification. Owners: [navigation](navigation.md).

## Sources and previews

- Use the shared persistent MD5-verified source cache. Populate processing misses from verified primary Backblaze storage. Enforce `documents.cache_max_gib` (default 50 GiB); evict least-recently-used completed sources to 90% when exceeded, protecting recent partial downloads and the current source.
- Preview detection uses pinned `yolov12l-doclaynet.pt`, CPU `imgsz=1024`, first/last three pages. Ignore page-header/footer/picture-only layouts. Select first useful front, last useful back, then next distinct front; persist actual page numbers and distinct roles. Zero/fewer than three qualifying pages is a completed outcome.
- Catalog requests/pages are the sole durable preview owner, retaining generation intent, leases, recipe, page count, and selected roles. Attempt counts and transient errors are local; the duplicate `library_book_previews` table is retired. Retain rendered workspaces; never prune previews automatically. Review-prefetched sources remain cached until ordinary eviction.

## Extraction and publication

Select `library.extract_non_pdf` in the interactive CLI. The retained `python -m app.modules.library.runtime.run_extract_non_pdf` entry point opens the same CLI; it no longer relies on subprocess environment or signal handlers. One worker processes each document through a safe checkpoint boundary. Launch does not start work automatically.

Pandoc and LibreOffice (`soffice`) are required at launch; MOBI additionally needs Calibre's `ebook-convert`. DOC/RTF and legacy PowerPoint Google fallbacks use `credentials/google-drive/personal_token.json` under the configured artifacts root. Missing optional tools/credentials produce per-item operational failures. No legacy repository credential fallback is used.

The CLI accepts `--limit`, `--per-mime-limit`, repeated `--only-md5`, and `--retry-known-failures` for this task. Cohort/retry options are shown in task details and saved with run options; changing worker/limit settings preserves the cohort. Reviewed full-catalog promotion remains an owner decision.


- Select unrestricted catalog documents with a verified `s3/primary` location and no active document cleanup plan. Read `yandex/source` paths and `s3/content` results from `catalog_locations`; publication inclusion and metadata do not gate extraction. Restricted/unknown privacy is ineligible for this public-output workflow.
- Extract verified non-PDF sources before language/classification decisions. Treat MIME as a hint; inspect byte signatures. OLE root streams distinguish DOC/PPT/XLS; nested objects do not determine outer format. Ambiguous/unreadable OLE remains unsupported. Persist verified DOC/PPT MIME against the unchanged source; XLS has no catalog correction.
- Preserve tables, LaTeX, and HTML figures in rich Markdown. Apply the shared Pandoc final formatter: LF, canonical headings/lists/fences, no prose wrapping, one terminal newline; preserve Unicode punctuation, literal text/code/LaTeX/HTML.
- Retain `unformatted.md`, `final.md`, converted sources/media/ASTs, validation reports, and an archive containing exactly final Markdown under `workspaces/library/non-pdf-extraction/run-<run-id>/<md5>/`. Emit a compact persisted `library.non_pdf_local_content` artifact before remote publication; include workspace paths in the summary.
- Every prepared image needs a public HTML reference. After local validation and its artifact event, acquire document/location locks and recheck their revisions, privacy, source verification, and cleanup eligibility before uploading. Keep those locks through object verification and the atomic content-location/result commit. MIME and recipe edits respect protected fields and write catalog audit revisions. Keep downstream metadata workspaces separate.
- New archives use `<md5>/run-<run-id>-<unique-generation>.zip`; images use the matching generation directory. The catalog owns the actual archive URL. Regeneration retains older outputs and cleans unexpected image keys only within the new generation, so failed publication cannot overwrite prior content. Broader remote retention/deletion belongs to guarded Maintenance work.
- QA cohorts are deterministic and capped per normalized MIME. Full-catalog promotion and known-source repairs need owner review. Use repeated `--only-md5` plus `--retry-known-failures` for exact reviewed retry cohorts; preserve prior outputs for comparison.
- Only deterministic verified-byte container/decoding/parser failures justify a guarded `corrupted` move. Unsupported/OCR-only content, converter timeouts, output-validation/storage failures, and missing tools are operational or deferred outcomes, never proof of corruption.
- Non-PDF successful outputs, unsupported decisions, verified MIME facts, and recipe versions are durable. Processing/failure/deferral state, attempt counts, run IDs, and errors are local SQLite; regeneration must retain prior durable output.
- Runs with operational failures or snapshot conflicts report failure; intact unsupported files and guarded corruption plans are handled outcomes. Deferred content reports a deferred outcome. Source workspaces, inspection events, progress snapshots, and the final summary retain their existing payload kinds.
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
