# Library metadata processing

## Extraction

- Select only documents with a verified primary-storage checkpoint. Reuse the MD5-verified source cache; populate misses only from configured Backblaze storage. Do not add Yandex, legacy S3, or compatibility source branches.
- Preserve the adopted prompt, Schema.org validation, PDF edge-page slicing, and normalization unless the owner requests a version change.
- Persist content failures after every model attempt and resume with the next untried model. Quota, service, storage, and stop conditions are retryable, not terminal exclusions.
- Defer a document whose models are exhausted and continue. Use `all_keys_exhausted` only when every configured model is unavailable.
- Metadata requires a non-placeholder title plus another bibliographic/content signal. Never overwrite usable `metadata.schema_org`; replace objectively poor metadata only with validated usable output. Never erase language with null, upload metadata ZIPs, or mutate storage URLs.

## Evaluation

- Preserve valid positive and negative evaluations. Reopen only missing results, applicable rows without classification, or non-applicable rows that still retain classification.
- Persist per-document/model failures and do not retry an already-failed model. A changed pool may reopen terminal failures.
- Usable responses require a concise reason and, when applicable, normalized DDC and category path. Malformed or incomplete responses advance to the next model.
- Publish processed/total counts, skips, terminal outcomes, and per-model attempts/successes. Log document MD5 and resolved model before each request.
