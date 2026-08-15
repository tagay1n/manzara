# Library flow guidance

These rules apply to `app/modules/library/`.

## Source cache and cleanup planning

- `~/.monocorpus/0_entry_point` is a shared persistent source-document cache, not a task artifact directory. Library tasks may verify, reuse, and populate it; generated and temporary outputs remain under `~/.manzara`.
- `library.prepare_document_cleanup` is planning-only. It may identify non-Tatar/non-document records and duplicate ISBNs, but never mutates documents or remote storage. Every duplicate-ISBN group requires explicit review; format or path flags may recommend a keeper but never queue automatic ISBN cleanup.

## PDF previews

- Preview state is PostgreSQL-backed and depends on page count: one page gets first; two pages get first/last; longer PDFs get first/second/last.
- Missing roles for short PDFs are complete, not partial. Never duplicate page previews.
- API and frontend consumers use manifest roles and actual page numbers, never infer semantics from compact S3 names.

## Metadata extraction

- `library.metadata_extract` selects only documents with a verified primary-storage checkpoint and reads bytes only from configured Backblaze buckets. Do not add Yandex Disk, legacy S3, local-cache, or compatibility source branches.
- Preserve the adopted prompt, Schema.org validation, PDF edge-page slicing, and normalization unless the owner requests a prompt/version change.
- Models come only from `gemini.model_pools.library_metadata_extraction`; there are no code defaults.
- Persist content-level model failures after every attempt and resume with the next untried model. Quota, service, storage, and stop conditions are retryable and never terminally exclude a document.
- If only one document's remaining models are exhausted, defer it and continue. Stop with `all_keys_exhausted` only when every configured model is unavailable; expose deferrals and unresolved count in progress/artifacts.
- Metadata is usable only with a non-placeholder title plus another bibliographic/content signal. Boilerplate-only, title-only, or title-missing responses advance to the next model.
- Never overwrite usable `metadata.schema_org`. Replace objectively poor metadata only with validated usable output; preserve it if all models fail. Never erase language with null, upload metadata ZIPs, or mutate storage URLs.

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
