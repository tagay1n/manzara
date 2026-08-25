# Library flow guidance

These rules apply to `app/modules/library/`.

## Source cache and cleanup planning

- `~/.monocorpus/0_entry_point` is a shared persistent source-document cache, not a task artifact directory. Library tasks may verify, reuse, and populate it; generated and temporary outputs remain under `~/.manzara`.
- `library.prepare_document_cleanup` is planning-only. It may identify non-Tatar/non-document records and duplicate ISBNs, but never mutates documents or remote storage. Every duplicate-ISBN group requires explicit review; format or path flags may recommend a keeper but never queue automatic ISBN cleanup.

## PDF previews

- Preview state is PostgreSQL-backed and depends on page count: one page gets first; two pages get first/last; longer PDFs get first/second/last.
- Missing roles for short PDFs are complete, not partial. Never duplicate page previews.
- Preview object roles are deterministic from page count and use compact S3 names; PostgreSQL stores document-level status, page count, and recipe version rather than a per-object manifest.
- Keep per-run and per-document preview workspaces under `~/.manzara` for owner inspection; never prune rendered preview files automatically.

## Non-PDF content extraction

- Extract every verified non-PDF source before language or Library classification. Reuse the shared MD5-verified cache and download misses only from primary Backblaze storage.
- Rich Markdown preserves tables, LaTeX math, and monocorpus-style HTML figures. Referenced images live in the configured public Backblaze content-images bucket.
- During extraction QA, use a deterministic cohort capped per normalized catalog MIME type so restarts cannot expand the sample. Promote the task to the full catalog only after owner review.
- Validate that every prepared image has a public HTML `<img>` reference before publishing a document archive. After a successful checkpoint, remove image objects outside that document's current expected key set.
- Keep converted documents, media, ASTs, logs, Markdown, and ZIP archives under the run workspace in `~/.manzara`; never prune them automatically.
- Existing legacy content is replaced only after every new public object is verified and the source snapshot still matches.

## Metadata extraction

- `library.metadata_extract` selects only documents with a verified primary-storage checkpoint. Reuse an MD5-verified document from the shared source cache first; on a miss, populate that cache only from configured Backblaze storage. Do not add Yandex Disk, legacy S3, or compatibility source branches.
- Preserve the adopted prompt, Schema.org validation, PDF edge-page slicing, and normalization unless the owner requests a prompt/version change.
- Models come only from `gemini.model_pools.library_metadata_extraction`; there are no code defaults.
- Persist content-level model failures after every attempt and resume with the next untried model. Quota, service, storage, and stop conditions are retryable and never terminally exclude a document.
- If only one document's remaining models are exhausted, defer it and continue. Stop with `all_keys_exhausted` only when every configured model is unavailable; expose deferrals and unresolved count in progress/artifacts.
- Metadata is usable only with a non-placeholder title plus another bibliographic/content signal. Boilerplate-only, title-only, or title-missing responses advance to the next model.
- Never overwrite usable `metadata.schema_org`. Replace objectively poor metadata only with validated usable output; preserve it if all models fail. Never erase language with null, upload metadata ZIPs, or mutate storage URLs.

## Metadata evaluation

- Evaluation models come only from `gemini.model_pools.library_metadata_evaluation` and run in configured order through the shared Gemini runtime.
- Preserve valid positive and negative evaluations. Ignore `lib_eval_method` when selecting work: reopen only missing evaluation results, applicable rows without a classification, or non-applicable rows that still retain a classification. Continue recording the method for new decisions as provenance.
- Persist content-level failures per document and model. Resume with the next untried model; a changed model set reopens terminal failures without retrying models that already failed.
- A usable response has a concise decision reason and, when applicable, a normalized DDC plus category path. Empty, malformed, or incomplete responses advance to the next model.
- Quota and service failures never become permanent document exclusions. Uploaded Gemini files use shared best-effort cleanup.
- Publish persisted progress with processed/total counts, rule skips, terminal outcomes, and per-model attempts/successes. Log the document MD5 and resolved model before every Gemini request.

## Collections

- Detection, Gemini validation, and explicit metadata application belong to the `collections` flow; general Library operations remain in `library`.
- Canonical collections and accepted memberships are authoritative. Detection and Gemini reruns create resumable proposals/verdicts and never mutate canonical state.
- Eligibility requires object-valued `metadata.schema_org` with a usable title. Exclude legislation and normalized legal genres before feature indexing.
- Paths, directories, filenames, and storage hierarchy are never evidence or prompt input.
- Match approved signatures before grouping coherent unmatched records into new proposals.
- A document has at most one canonical collection; conflicts require owner resolution.
- Only explicit owner approval creates collections/memberships. New collections require two approved documents; existing-collection attachments may contain one.
- Keep `library.collection_apply` separate from proposal approval.
- Validation uses `gemini.model_pools.library_collection_validation`, one verdict per batch, no consensus voting.
- Batches adapt by model: start at or below 20, retry timeout/malformed output twice, then reduce `20 -> 10 -> 5 -> 2 -> 1`. `400`, `429`, blackout, and `5xx` do not change size.
- Validate response MD5 sets exactly. Missing, duplicated, unknown, or malformed results are response failures.
