# PostgreSQL catalog contract

## Status and adaptation gap

The owner reports that the normalized catalog migration is complete and the backend remains broken because adaptation is unfinished. The local, untracked `catalog-migration-buffer.sql` records completed data migration and physical-table retirement; it is a read-only status query, not a tracked schema definition. No live database was inspected during documentation cleanup.

The retired physical relations are `document`, `metadata`, `classification`, `normalization_canonicals`, `normalization_aliases`, `library_collections`, and `library_collection_items`. Their names are adapter views over normalized catalog relations. Views store no duplicate domain rows. Legacy upserts use the strict `catalog_upsert` command because views lack unique indexes.

Some adaptation exists, including personality candidate reads from normalized names/credits and command adapters. Other code still uses legacy projections: `app/modules/library/normalization_queries.py`, `app/modules/library/runtime/models/`, and Maintenance sync repositories are starting points for the audit. A remaining legacy query is evidence to inspect, not proof that every such query fails.

Do not rerun import or retirement to fix backend behavior. Verify the deployed relations, revisions, constraints, and import manifest read-only, then repair one backend operation at a time. Current catalog integration tests are absent; successful migration does not establish worker readiness. Testing follows the explicit-request policy in root instructions.

## Durable model

| Object | Responsibility |
| --- | --- |
| Publication | Edition, translation, or issue; inclusion, bibliographic fields, languages, identifiers, subjects/audiences, credits, source-work references, one classification and primary collection |
| Document | Stable MD5 file identity; selected file, completeness, restrictions, MIME, accessibility, storage locations |
| Entity / name / alias / contribution | People and organizations, observed spellings, identity aliases, and contextual credit resolution |
| Classification | Parent-child taxonomy with stable legacy classification IDs; generated paths/DDC |
| Collection | Grouping and independent publication switch |
| Preview request / page | Durable request, lease, successful generations and selected assets |
| Evidence / proposal / revision | Source evidence, reviewed snapshots, pending changes and transactional audit |

A publication may have multiple files; ISBN duplicates do not establish equivalence. Grouping requires reviewed revisions and explicit conflict resolutions. Inclusion never overrides file privacy or completeness; unknown privacy fails closed.

Human confirmation is separate from entity active/merged status and AI success. A spelling can belong to multiple identities. Linking an alias must not resolve every matching mention; contribution resolution identifies the reviewed occurrence. Publisher types come from publisher mentions, then publisher aliases.

Editable metadata lives in columns/relations; Schema.org is generated transport. JSONB holds evidence, snapshots, proposals, manifests, and audit revisions. Retained durable workflow tables own their checkpoints. Sparse import evidence and the verified external backup preserve provenance without duplicating every source payload.

## Owners and mutation rules

- Schema: Alembic versions and frozen SQL under `alembic/`; Python table metadata in `app/catalog/schema.py`.
- Shared domain API: `app/catalog/repository.py`, `contracts.py`, `metadata_store.py`, `identities.py`, `grouping.py`, and `previews.py`.
- Independent admin factory: `create_catalog_admin_app` in `app/catalog_admin.py`; routes under `/api/catalog`. The host supplies authentication, engine lifecycle, and authenticated private preview delivery. It does not initialize Manzara workflows or migrations.
- Mutations check reviewed revisions and write audit records transactionally; stale commands return HTTP 409. Metadata updates need publication and document revisions. Identity/alias commands verify affected identities and mentions.
- Admin edits protect fields from later automation. Protected publication changes enter metadata review; file/privacy and collection conflicts fail for explicit resolution. Deleting aliases preserves source names and explicitly resolved mentions.

## Previews and export

Catalog preview leases and claim tokens are durable. A failed regeneration retains the previous success; expired claims can be reclaimed. Restricted outputs require private storage and authenticated delivery. Restricting a file disables its prior public generation in the API; deletion of old public objects remains guarded Maintenance work.

The catalog preview worker uses MD5-verified sources and the pinned page detector. Static export includes only eligible selected files and confirmed contextual identities; private generations are excluded. See Library [document](../app/modules/library/guidance/documents.md) and [export](../app/modules/library/guidance/site-export.md) contracts. Compatibility of the remaining workers/export with the deployed catalog still needs verification.

## Recovery tooling

`scripts/catalog_import.py`, `scripts/catalog_manual_*.py`, `app/catalog/importer.py`, and `app/catalog/cutover.py` remain implementation/recovery tools. Their completed manual-stage handoffs are removed. Consult code and persisted manifests for an explicitly planned recovery, with a restore-tested backup and writers paused. Never infer committed state from an editor HTTP response. Preserve saved index definitions and verify restoration before restarting writers.

[Backup and recovery](postgres-backup-recovery.md) covers portable dumps and isolated restore drills. Any new persisted-data migration or compatibility choice requires owner agreement.
