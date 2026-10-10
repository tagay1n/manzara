# Static Library export

The separate public site consumes only `library.site_export` bundles, never Manzara tables. The task requires complete inventory, rejects source/retry controls, checks catalog schema/revision read-only, and mutates no catalog/remote storage. Only public endpoint/bucket settings are needed, without credentials. Owners: `site_export.py`, `site_export_repository.py`, `runtime/run_site_export.py`.

## Eligibility and publication

Export complete unrestricted selected files of included, unmerged publications with verified primary locations in the configured public bucket and no active cleanup. Verification means catalog checkpoints, not remote probes. One read-only repeatable-read snapshot includes ordered bibliography/accessibility/references, identities, taxonomy, collections, and successful preview assets. Collection publication is independent; content URLs require verified configured content-bucket locations.

Exclude source paths/upstream evidence, private/encrypted/signed URLs, credentials/checksums/workflow state. Invalid metadata remains repair state; restricted previews never export. Manzara owns eligibility, Schema.org validation, contextual identity resolution, joins, and public URLs.

Stage beside artifact `durable/library/site-exports/`, verify archive members/metadata/counts/checksums/unique IDs/paths/relation targets, then atomically replace `library-export-v1.tar.gz`. Stop/failure before replacement retains prior export; after replacement preserves the new bundle. Snapshot reads check stop between queries/batches with a 60-second statement timeout; preparation uses safe boundaries. Summary records path/checksum/revision/counts/exclusions.

## Bundle v1

Stable member order: `manifest.json`, `documents.jsonl`, `entities.jsonl`, `collections.jsonl`, `classifications.jsonl`, `redirects.jsonl`. Manifest identifies `manzara-library-export` version 1, metadata contract, counts, SHA-256, semantic revision, exclusions; records/arrays are deterministic.

Documents use `work` for JSON-LD and `file`/`relations`/`facets`/optional `preview` for build data. IDs are opaque namespaced strings; stable MD5/short suffix distinguish paths. Contributor/publisher routes remain role projections of `catalog_entities`; contributor role never changes organization kind. Only confirmed active entities plus confirmed contributions receive canonical names/entity relations; unresolved credits retain observed names. Alias lists contain confirmed aliases and never resolve credits by spelling. At most one collection/classification per document. Latest successful public previews survive failed regeneration.

Additive optional fields/ignorable records are compatible; removed fields/semantic changes require a new version. `site_export.py:verify_export_bundle` verifies an existing bundle without database/storage/site build access.
