# Library document processing

Owners: [navigation](navigation.md). Common logging/artifact/lifecycle rules are in the nearest `AGENTS.md`; [operations](../../../../docs/operations.md#local-storage) owns paths/cache configuration.

## Sources and previews

`library.generate_book_previews` uses one discovery loop, querying at most 200 MD5-ordered candidates per iteration. PostgreSQL joins publications, primary locations, latest requests, successful generations, live leases, and cleanup exclusions; there is no inventory load, global count, or per-document discovery lookup. An empty batch ends the loop. The cursor advances past each fetched batch so failed/busy documents cannot spin in one run; a later run can revisit them. `--limit` caps fetched eligibility before request creation across all batches; repeated `--only-md5` restricts sources; `--retry-known-failures` creates new requests while preserving generations. Progress totals count discovered candidates so far.

Eligible sources are included complete unrestricted PDFs of unmerged publications, regardless of selection. Completeness means whole-publication content, not download integrity. Require verified `s3/primary` with known size and no active document cleanup plan. Recheck source/eligibility before publication.

Skip current-recipe public successes, including zero selected pages, and known failures unless explicitly retried. Pending/expired claims resume with fresh tokens at document boundaries; live claims skip. Claim only the selected cohort. Catalog requests/pages own intent, leases, recipe, page count, and selected roles; attempts/errors are local.

Startup schema/key checks are aggregated in one read-only PostgreSQL query. Under the document session lock, one implicit transaction/message validates the source, creates or resumes intent and claims it. Each upload uses one message on that same session to recheck public eligibility/source revisions and renew the lease; CPU/read boundaries renew only when one third of the configured lease has elapsed. Success uses one message for locked source/lease guards, bulk page insertion and ready status, including zero-page success. All SQL transactions close before remote work. Failure finalization is guarded separately and cannot replace a newer claim or success. Unknown write acknowledgements discard the session and stop the run without replay; later discovery reconciles durable state. No schema installation or migration is performed by the task.

Use configured primary storage/cache and dedicated public `documents.primary_storage.bucket.book_previews`. Pool size must be at least two. Detector packages are pinned in `requirements.txt`; the YAML-selected checkpoint downloads on the first nonempty run. Empty cohorts initialize no model/storage clients.

- Populate source-cache misses only from MD5-verified Backblaze primary storage. Exceeding `documents.cache_max_gib` evicts completed least-recently-used entries to `documents.cache_target_percent`, protecting current sources/recent partials.
- Detection uses pinned `yolov12l-doclaynet.pt`, CPU `imgsz=1024`, first/last three pages. Ignore header/footer/picture-only layouts; choose first useful front, last useful back, then next distinct front. Persist page numbers/distinct roles; fewer than three or zero useful pages is success.
- Upload immutable request/claim keys and verify all objects before publishing roles. Item failures continue and fail the run; shared storage/model/database failure stops it. Safe stop completes the current document. Retain previous/abandoned outputs and rendered workspaces for explicit maintenance review; no automatic preview pruning.

## Extraction and publication

`library.extract_non_pdf` accepts `--limit`, `--per-mime-limit`, repeated `--only-md5`, and `--retry-known-failures`; options persist and `/settings` preserves cohort controls. Full-catalog promotion and known-source repairs require owner review. Exact retry cohorts use MD5/retry flags and retain old outputs for comparison.

Pandoc/LibreOffice (`soffice`) are required at launch; MOBI also requires Calibre `ebook-convert`. Google DOC/RTF/PPT fallbacks use artifact `private/credentials/google-drive/personal_token.json`. Missing optional tools/credentials fail per item; no repository credential fallback.

- Select unrestricted documents with verified `s3/primary`, no active cleanup, and known privacy; inclusion/metadata do not gate extraction. Source/content locations come from `catalog_locations`.
- Extract before language/classification decisions. Inspect byte signatures rather than trusting MIME. OLE root streams distinguish DOC/PPT/XLS; nested objects never determine outer format. Ambiguous/unreadable OLE is unsupported. Correct verified DOC/PPT MIME against the unchanged source; no XLS catalog correction.
- Preserve tables, LaTeX, and HTML figures. Shared Pandoc formatting uses LF, canonical headings/lists/fences, no prose wrapping, one terminal newline, and preserved Unicode/literal code/LaTeX/HTML.
- Retain `unformatted.md`, `final.md`, converted sources/media/ASTs, validation reports, and an archive containing exactly final Markdown under `workspaces/library/non-pdf-extraction/run-<run-id>/<md5>/`. Save `library.non_pdf_local_content` before remote publication and link workspaces in summaries.
- Every prepared image needs a public HTML reference. After validation/artifact save, lock document/locations and recheck revisions/privacy/source/cleanup before upload. Hold locks through verification and atomic content-location/result commit. MIME/recipe writes honor protection and advance revisions; metadata workspaces stay separate.
- Verify anonymous HTTP access to each image and the content archive before catalog publication. Retry connection/timeouts and HTTP 403/404/408/425/429/5xx within YAML `non_pdf.link_total_attempts` (1–10, including the initial request), using exponential delays from `link_retry_backoff_seconds` (1–60), capped at 60 seconds. Other HTTP/request failures fail immediately. Close every response; log retries and retain the final HTTP status or exception type without URLs in failure context. A failed access check is operational, never evidence of source corruption.
- Archives use `<md5>/run-<run-id>-<unique-generation>.zip`; images share that generation directory. Catalog stores actual URLs. Never overwrite prior content; unexpected-image cleanup is limited to the new generation and preserves every expected image key. Inventory visible and hidden unexpected keys, permanently delete their explicit data versions before delete markers, and verify that no history remains. Storage credentials require version-listing and version-deletion permissions. Broader deletion belongs to guarded Maintenance.
- Only deterministic verified-byte container/decoding/parser failures justify `corrupted` plans. Unsupported/OCR-only content, timeouts, validation/storage failures, and missing tools are not corruption.
- Successes/unsupported decisions/verified MIME/recipes are durable; attempts/run IDs/errors/deferrals are local. Operational failures/snapshot conflicts fail the run; intact unsupported files/guarded corruption plans are handled; deferred content yields a deferred outcome.

## Converter contracts

| Format | Behavior |
| --- | --- |
| DOC/RTF | LibreOffice first; normalize converted DOCX ZIP metadata. Google fallback only after failed/invalid conversion; delete temporary Drive files. Word owner files are invalid. |
| Large DOCX | Stream oversized table-free body XML paragraphs/images to HTML; otherwise use normal reader. Omit empty images with workspace evidence. |
| PPTX | Visible slides, coordinate order/stable fallback, native text/lists/tables. Defer whole deck for visible pictures/fills/backgrounds, charts, SmartArt, embedded objects/equations, unsupported visuals, or no native text. Ignore hidden slides/notes/unreferenced master media. No OCR/generated descriptions. |
| Legacy PPT | Stage detected `.ppt`, convert locally to PPTX; Google Slides fallback after failed/invalid/text-empty conversion, always deleting remote temporaries. Preserve slide boundaries; ambiguous drawing relations defer. |
| XLS/XLSX | Correct suffix, LibreOffice HTML; visible sheet headings/tables, no navigation/images/charts/formula expressions. Displayed values may calculate without source caches. |
| ODT | Verify ZIP, LibreOffice HTML, preserve prose/headings/tables/sidecar images; shared formatter. |
| MOBI | Verify BOOKMOBI, Calibre EPUB conversion, Pandoc parsing; retain source identity, no Google fallback. |
| HTML | Declared charset with verified legacy fallback; flatten layout tables, preserve data tables and normalized UTF-8 source. |

PPTX saves `pptx-inspection.json`/compact artifacts; counts distinguish inspected/extracted/image/unsupported/empty decks and visual counts may overlap. Oversized XML yields incomplete inspection, never a completed-inspection count. Deferrals retain content and reopen only on explicit retry/new recipe.

Recipes live in `non_pdf_types.py`. YAML `non_pdf`, `djvu`, `metadata`, `previews`, and `network` own deadlines/limits/evidence/rendering/lease policies; changes affect newly performed work under checkpoint rules.
