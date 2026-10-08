# PostgreSQL catalog contract

## Status and adaptation gap

Database-only normalization through Alembic revision `20261007_0061` is applied in the owner's PostgreSQL catalog. Deployed relations, constraints, derived keys, audit revisions, and sampled read envelopes were inspected. The CLI personality task and cleanup planner/review commands use catalog-native reads/writes; other workflows remain disabled pending adaptation. Credential-backed execution has not established task readiness. The local, untracked `catalog-migration-buffer.sql` records completed data migration and physical-table retirement; it is a read-only status query, not a tracked schema definition.

The retired physical relations are `document`, `metadata`, `classification`, `normalization_canonicals`, `normalization_aliases`, `library_collections`, and `library_collection_items`. Their names are adapter views over normalized catalog relations. Views store no duplicate domain rows. Legacy upserts use the strict `catalog_upsert` command because views lack unique indexes.

Personality normalization reads normalized names/credits/languages and persists atomic identity hypotheses through `app/catalog/personality_normalization.py`. Other code still uses legacy projections: `app/modules/library/normalization_queries.py`, `app/modules/library/runtime/models/`, and Maintenance sync repositories are starting points for the audit. A remaining legacy query is evidence to inspect, not proof that every such query fails.

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

## Relational normalization: revisions 0060 and 0061

These revisions define the current database contract. For another environment, inspect its deployed Alembic revision read-only rather than inferring deployment from the checkout. Backend and recovery-tool adaptation is separate work and is not included in these database changes.

Revision 0060 validates credit-slot uniqueness, distinct entity/name membership within proposals, alias and contribution states, confirmed contribution targets, age ranges, and self-reference/merge-status rules. Reference authors belong to `catalog_references`, with cascading deletion and publication-key updates. Three known unique constraints duplicating primary keys are removed only after checking their equivalence; unexpected dependencies abort the migration.

Revision 0061 decomposes the remaining domain lists and credit labels:

| Relation | Identity and fact |
| --- | --- |
| `catalog_publication_languages` | Publication and position identify one language |
| `catalog_document_access_modes` | Document and position identify one accessibility mode |
| `catalog_sufficient_modes` / `catalog_sufficient_mode_items` | Document and group position identify a sufficient-mode group; ordered child rows identify its modes |
| `catalog_reference_urls` | Source-work reference and position identify one URL |
| `catalog_credit_groups` | Publication, role, and outer position identify the group and its role label |
| `catalog_contributions` | Stable contribution ID identifies a credited name/entity at a unique nested position within its group |

Migration preserves list order, repeated values, empty sufficient-mode groups, and absent versus empty reference URL lists (`urls_present`). It verifies reconstruction against every old list and label before dropping the old columns with `RESTRICT`. Conflicting data aborts the transaction; it never infers merges or discards conflicting rows. Publication and contribution IDs, source evidence, review snapshots, and existing historical revisions are retained. One primary collection and one source-work reference per publication remain intentional cardinalities. `has_metadata` and `metadata_present` retain their distinct meanings.

Ordered child relations address the first-normal-form concern of storing independently editable domain lists in array columns. Moving the role label to its credit group removes its dependency on a group key repeated across contributions. This improves normalization without claiming strict BCNF for the whole catalog: stored search keys are intentional derived values, and JSONB evidence, snapshots, proposals, and audit payloads retain their document semantics.

PostgreSQL 18 with UTF-8 owns derived keys through `catalog_name_key`, `catalog_title_key`, and `catalog_identifier_key`, enforced on every insert/update by database triggers. Names use Unicode case folding with the built-in `pg_unicode_fast` collation; collection titles additionally replace runs outside the retained Latin/Tatar-Cyrillic character allowlist with a space and trim it. ISBN keys remove characters other than digits and `X` after uppercasing; other identifier kinds retain their value. These keys are controlled derived values, not separate identity claims or uniqueness rules. Backfill records old/new values in audit revisions and advances affected entity/collection revisions, so previously reviewed snapshots may require renewed review.

Database statement locks and row triggers prevent taxonomy and merge cycles, and enforce the taxonomy's shared DDC rule. Reference URL presence is protected against contradictory child rows.

Read projections `catalog_publication_metadata`, `catalog_document_metadata`, `catalog_sufficient_mode_metadata`, `catalog_reference_metadata`, and `catalog_contribution_metadata` reconstruct the previous envelope without storing duplicate domain rows. Installed Schema.org and legacy read views use these projections. Scalar projected fields remain automatically updatable, but collection-valued fields and role labels require writes to the normalized child relations. Existing backend/import helpers that still write the former arrays or per-contribution labels need adaptation before use; these migrations do not establish their readiness.

## Owners and mutation rules

- Schema: Alembic versions and frozen SQL under `alembic/`; Python table metadata in `app/catalog/schema.py`.
- Shared domain API: `app/catalog/repository.py`, `contracts.py`, `metadata_store.py`, `identities.py`, `grouping.py`, and `previews.py`.
- HTTP operations and the independent catalog admin API are retired. Shared catalog commands remain in `app/catalog/`; the CLI exposes normalization, cleanup planning/review, and run inspection, with other operations awaiting later slices.
- Mutations check reviewed revisions and write audit records transactionally; stale commands raise `CatalogConflict`. Metadata updates need publication and document revisions. Identity/alias commands verify affected identities and mentions.
- Admin edits protect fields from later automation. Protected publication changes enter metadata review; file/privacy and collection conflicts fail for explicit resolution. Deleting aliases preserves source names and explicitly resolved mentions.

## Previews and export

Catalog preview leases and claim tokens are durable. A failed regeneration retains the previous success; expired claims can be reclaimed. Restricted outputs require private storage and authenticated delivery. The retained preview domain logic excludes prior public generations when a file becomes restricted; deletion of old public objects remains guarded Maintenance work.

The catalog preview worker uses MD5-verified sources and the pinned page detector. Static export includes only eligible selected files and confirmed contextual identities; private generations are excluded. See Library [document](../app/modules/library/guidance/documents.md) and [export](../app/modules/library/guidance/site-export.md) contracts. Compatibility of the remaining workers/export with the deployed catalog still needs verification.

## Recovery tooling

`scripts/catalog_import.py`, `scripts/catalog_manual_*.py`, `app/catalog/importer.py`, and `app/catalog/cutover.py` remain implementation/recovery tools. Their completed manual-stage handoffs are removed. Consult code and persisted manifests for an explicitly planned recovery, with a restore-tested backup and writers paused. Never infer committed state from a client response. Preserve saved index definitions and verify restoration before restarting writers.

[Backup and recovery](postgres-backup-recovery.md) covers portable dumps and isolated restore drills. Any new persisted-data migration or compatibility choice requires owner agreement.
