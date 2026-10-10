# Static Library export contract

The separate public static site is distinct from the removed Manzara web frontend. Its only supported database boundary is `library.site_export`: consume the versioned bundle, never query Manzara tables directly.

Launch `python -m app --task library.site_export`, then enter `/run`. Export runs with one worker and requires the complete inventory; candidate limits and source/retry controls are rejected. It reads the normalized catalog after a read-only column/revision preflight and makes no catalog or remote-storage mutations. Only the public endpoint/bucket configuration is needed; storage and Yandex credentials are not used.

Logs use the shared terminal/stdout sink without a verbose `.log` file. Tasks emit no events; progress uses the local run row, and the shared runner directly saves the final summary artifact. `/summary` includes the bundle path, checksum, revision, published counts and exclusion reasons. Live CLI execution and full bundle publication remain unverified.

## Eligibility and publication

Manzara owns privacy/eligibility, Schema.org validation, confirmed contextual identities, classification/collection joins, and public URLs. Export only complete unrestricted selected files of included, unmerged publications with a verified primary location in the configured public bucket and no active document cleanup plan. Verification is the catalog checkpoint; export does not probe remote objects. Read candidates, ordered bibliographic/accessibility/reference relations, identities, taxonomy, and successful preview assets in one read-only repeatable-read transaction. Collection publication remains an independent switch. Content URLs require a verified location in the configured content bucket.

Exclude source paths, upstream evidence, private/encrypted/signed URLs, credentials, storage-sync checksums, and workflow state. Invalid metadata remains repair state. Restricted previews never export; failed regeneration retains the prior successful generation.

Prepare and verify the full bundle in a staging directory beside `durable/library/site-exports/` beneath the configured artifact root, then atomically replace its `library-export-v1.tar.gz` file. Checks include archive members, metadata, counts, checksums, unique IDs/paths, and relation targets. Stops/failures before the final replacement retain the prior export; a stop after replacement leaves the completed bundle published. Snapshot reads check for stops between queries/batches, with a 60-second statement timeout; document processing and preparation stop at safe boundaries.

## Bundle v1

`library-export-v1.tar.gz` contains, in stable order: `manifest.json`, `documents.jsonl`, `entities.jsonl`, `collections.jsonl`, `classifications.jsonl`, `redirects.jsonl`.

The manifest identifies `manzara-library-export` version 1, metadata contract, counts, SHA-256 checksums, semantic revision, and exclusions. Records/arrays use deterministic ordering.

Documents put JSON-LD under `work`, build data under `file`/`relations`/`facets`/optional `preview`. IDs are opaque namespaced strings; MD5 remains stable and a short suffix distinguishes document paths. Bundle v1 retains its contributor/publisher namespaces and routes as role projections of `catalog_entities`; an organization's contributor role does not turn its `kind` into a person. Only confirmed active entities and explicitly confirmed contributions receive entity relations or canonical display names. Unresolved credits retain their observed names; aliases never resolve a credit by name matching. Entity alias lists include only confirmed aliases. At most one exported collection and classification belong to a document. Previews use the latest successful public generation, preserving a prior success after a failed regeneration.

Additive optional fields/ignorable records are compatible; removed fields or changed semantics require a new version. `verify_export_bundle` in `site_export.py` can verify an existing bundle without database/storage access or the site build. Owners: `site_export.py`, `site_export_repository.py`, `runtime/run_site_export.py`.
