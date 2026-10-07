# Static Library export contract

The separate public static site is distinct from the removed Manzara web frontend. Its only supported database boundary is `library.site_export`: consume the versioned bundle, never query Manzara tables directly. Remaining export code needs compatibility verification against the migrated catalog.

## Eligibility and publication

Manzara owns privacy/eligibility, Schema.org validation, confirmed contextual identities, classification/collection joins, and public URLs. Export only complete unrestricted selected files with a verified primary object in the configured public bucket. Read candidates and identities in one repeatable-read transaction.

Exclude source paths, upstream evidence, private/encrypted/signed URLs, credentials, storage-sync checksums, and workflow state. Invalid metadata remains repair state. Restricted previews never export; failed regeneration retains the prior successful generation.

Prepare and validate the full bundle before replacing `durable/library/site-exports/` beneath the configured artifact root. Stops/failures retain the prior export.

## Bundle v1

`library-export-v1.tar.gz` contains, in stable order: `manifest.json`, `documents.jsonl`, `entities.jsonl`, `collections.jsonl`, `classifications.jsonl`, `redirects.jsonl`.

The manifest identifies `manzara-library-export` version 1, metadata contract, counts, SHA-256 checksums, semantic revision, and exclusions. Records/arrays use deterministic ordering.

Documents put JSON-LD under `work`, build data under `file`/`relations`/`facets`/optional `preview`. IDs are opaque namespaced strings; MD5 remains stable and a short suffix prevents path collisions. Only confirmed active identities and confirmed mentions receive entity relations; unresolved names remain document text. At most one exported collection and classification belong to a document.

Additive optional fields/ignorable records are compatible; removed fields or changed semantics require a new version. Keep bundle verification independent from the site build. Owners: `site_export.py`, `site_export_repository.py`, `runtime/run_site_export.py`.
