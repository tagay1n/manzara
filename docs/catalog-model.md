# PostgreSQL catalog contract

## Status and adaptation gap

Alembic revision `20261008_0062` is applied in the owner's PostgreSQL catalog. Deployed relations, constraints, derived keys, audit revisions, and sampled read envelopes were inspected. The CLI personality/non-PDF/book preview/static export/publisher proposal/collection discovery/metadata extraction/evaluation tasks and cleanup review commands, standalone daily cleanup preparation/Yandex Sync, and workflow-only Backblaze transfer use catalog-native reads/writes; all registered interactive tasks have catalog-native handlers. Credential-backed execution has not established task readiness. The active Alembic history is a single baseline at that same revision. `alembic/sql/baseline_0062.sql` freezes the current schema; bootstrap and historical upgrade policy live in [operations](operations.md).

The retired physical relations are `document`, `metadata`, `classification`, `normalization_canonicals`, `normalization_aliases`, `library_collections`, and `library_collection_items`. Their names are adapter views over normalized catalog relations. Views store no duplicate domain rows. Legacy upserts use the strict `catalog_upsert` command because views lack unique indexes.

Personality normalization reads normalized names/credits/languages and persists atomic identity hypotheses through `app/catalog/personality_normalization.py`. Backblaze transfer reads normalized document/source/primary snapshots and writes audited primary-location checkpoints through `app/catalog/document_transfer.py`, including repair of incomplete checkpoints. Its live execution remains unverified. Publisher clustering reads normalized entities, names, linked alias reviews, and publisher contributions, retaining the existing alias suppression rule. It writes catalog proposals and versioned response checkpoints without changing identities or credits; review/apply remains deferred. Metadata extraction/evaluation use normalized publication/document snapshots and audited metadata commands; remaining legacy helpers such as `app/modules/library/normalization_queries.py` and `app/modules/library/runtime/models/` still need auditing before reuse. A remaining legacy query is evidence to inspect, not proof that every such query fails.

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

Sync creates one pending publication per new file MD5, preserves existing publication membership, and never infers edition equivalence. Empty publications remain after document cleanup. A publication may have multiple files; ISBN duplicates do not establish equivalence. Grouping requires reviewed revisions and explicit conflict resolutions. Inclusion never overrides file privacy or completeness; unknown privacy fails closed.

Human confirmation is separate from entity active/merged status and AI success. A spelling can belong to multiple identities. Linking an alias must not resolve every matching mention; contribution resolution identifies the reviewed occurrence. Publisher types come from publisher mentions, then publisher aliases.

Editable metadata lives in columns/relations; Schema.org is generated transport. JSONB holds evidence, snapshots, proposals, manifests, and audit revisions. Retained durable workflow tables own their checkpoints. Sparse import evidence and the verified external backup preserve provenance without duplicating every source payload.

Collection discovery reads normalized publications and contextual entity/name contributions and writes proposal-only hypotheses into `catalog_proposals`. Collection validation/apply tasks and the former review workbench are removed; historical workflow data is preserved without conversion. [Collection discovery](../app/modules/library/guidance/collections.md) owns input, refresh, and output contracts. Live execution remains unverified.

## Operational ownership

The current schema omits five retired physical PostgreSQL relations: `library_collection_document_features`, `library_metadata_quality_state`, `library_collection_validation_attempts`, `library_book_previews`, and `publisher_merge_proposals`. Retained quality caches and validation attempts use local SQLite; collection discovery now derives publication features in memory and no longer uses the retired collection feature/validation stores. Catalog preview requests/pages and catalog publisher proposals already contain the reconciled durable records; `catalog_selected_previews` and `catalog_publisher_proposals` provide projections without duplicate rows. Publisher drafts use catalog proposal IDs.

Personality decisions, explicit reviewed retries, accepted identities, and identity conflicts remain durable. Their model exclusions and processing/failure/deferral checkpoints are local. Non-PDF results, unsupported reasons and verified MIME facts remain durable; attempts and errors are local. Cleanup plans/reviews/phases remain durable; counts/run IDs/errors are local. Preview generation intent/leases remain durable; errors are local. There is no ongoing dual write or store fallback.

Fresh bootstrap does not replay the completed storage cutover. Older-schema transfers and recovery use the historical checkout under [operations](operations.md). Retained legacy import renderers refuse this catalog rather than recreate retired owners. Workflow enablement remains subject to the adaptation gap above.

Publication-based metadata processing and its retry/review boundaries are documented in the Library [metadata contract](../app/modules/library/guidance/metadata.md). Both CLI tasks use terminal-only logging and disable task lifecycle events while retaining local run/progress state and structured result artifacts. Live execution remains unverified.

## Relational normalization

The baseline defines normalized domain relations and durable workflow ownership. For another environment, inspect its deployed Alembic revision read-only rather than inferring deployment from the checkout. Backend and recovery-tool adaptation remains separate work.

Constraints enforce credit-slot uniqueness, distinct entity/name membership within proposals, alias and contribution states, confirmed contribution targets, age ranges, and self-reference/merge-status rules. Reference authors belong to `catalog_references`, with cascading deletion and publication-key updates. Primary keys provide identity uniqueness without duplicate unique constraints.

Domain lists and credit labels use these relations:

| Relation | Identity and fact |
| --- | --- |
| `catalog_publication_languages` | Publication and position identify one language |
| `catalog_document_access_modes` | Document and position identify one accessibility mode |
| `catalog_sufficient_modes` / `catalog_sufficient_mode_items` | Document and group position identify a sufficient-mode group; ordered child rows identify its modes |
| `catalog_reference_urls` | Source-work reference and position identify one URL |
| `catalog_credit_groups` | Publication, role, and outer position identify the group and its role label |
| `catalog_contributions` | Stable contribution ID identifies a credited name/entity at a unique nested position within its group |

Ordered child rows retain list order, repeated values, empty sufficient-mode groups, and absent versus empty reference URL lists (`urls_present`). Stable publication and contribution IDs, source evidence, review snapshots, and historical revisions remain durable. One primary collection and one source-work reference per publication remain intentional cardinalities. `has_metadata` and `metadata_present` retain their distinct meanings.

Ordered child relations address the first-normal-form concern of storing independently editable domain lists in array columns. Moving the role label to its credit group removes its dependency on a group key repeated across contributions. This improves normalization without claiming strict BCNF for the whole catalog: stored search keys are intentional derived values, and JSONB evidence, snapshots, proposals, and audit payloads retain their document semantics.

PostgreSQL 18 with UTF-8 owns derived keys through `catalog_name_key`, `catalog_title_key`, and `catalog_identifier_key`, enforced on every insert/update by database triggers. Names use Unicode case folding with the built-in `pg_unicode_fast` collation; collection titles additionally replace runs outside the retained Latin/Tatar-Cyrillic character allowlist with a space and trim it. ISBN keys remove characters other than digits and `X` after uppercasing; other identifier kinds retain their value. These keys are controlled derived values, not separate identity claims or uniqueness rules. Reviewed snapshots must match the current entity/collection revisions before mutation.

Database statement locks and row triggers prevent taxonomy and merge cycles, and enforce the taxonomy's shared DDC rule. Reference URL presence is protected against contradictory child rows.

Read projections `catalog_publication_metadata`, `catalog_document_metadata`, `catalog_sufficient_mode_metadata`, `catalog_reference_metadata`, and `catalog_contribution_metadata` reconstruct the previous envelope without storing duplicate domain rows. Installed Schema.org and legacy read views use these projections. Scalar projected fields remain automatically updatable, but collection-valued fields and role labels require writes to the normalized child relations. Existing backend/import helpers that still write the former arrays or per-contribution labels need adaptation before use; schema bootstrap does not establish their readiness.

## Owners and mutation rules

- Schema: Alembic versions and frozen SQL under `alembic/`; Python table metadata in `app/catalog/schema.py`.
- Shared domain API: `app/catalog/document_sync.py` for file discovery/deletion, `app/catalog/repository.py`, `contracts.py`, `metadata_store.py`, `identities.py`, `grouping.py`, `previews.py`, and `book_previews.py`.
- HTTP operations and the independent catalog admin API are retired. Shared catalog commands remain in `app/catalog/`; the CLI exposes normalization, non-PDF extraction, book previews, static export, publisher proposal generation, collection discovery, metadata extraction/evaluation, cleanup review, and run inspection. Standalone daily maintenance owns cleanup preparation and Yandex Sync, with no remaining disabled interactive tasks.
- Non-PDF extraction reads unrestricted files and verified primary locations through `app/catalog/non_pdf.py`. MIME corrections and content generations use document/location snapshots, row locks, field protections, and transactional revision audits; retry state remains local. See the Library [extraction contract](../app/modules/library/guidance/documents.md#extraction-and-publication).
- Mutations check reviewed revisions and write audit records transactionally; stale commands raise `CatalogConflict`. Metadata updates need publication and document revisions. Identity/alias commands verify affected identities and mentions.
- Admin edits protect fields from later automation. Protected publication changes enter metadata review; file/privacy and collection conflicts fail for explicit resolution. Deleting aliases preserves source names and explicitly resolved mentions.

## Previews and export

Catalog preview leases and claim tokens are durable. A failed regeneration retains the previous success; expired claims can be reclaimed. Restricted outputs require private storage and authenticated delivery. The retained preview domain logic excludes prior public generations when a file becomes restricted; deletion of old public objects remains guarded Maintenance work.

The automatic book-preview CLI task reads normalized publications/documents/locations through `app/catalog/book_previews.py`, uses MD5-verified sources and the pinned page detector, and scopes durable claims to its eligible cohort. Preview-specific events are retired; shared CLI run lifecycle and final summaries remain. The static export CLI task reads normalized domain relations in one read-only snapshot, includes only eligible selected files and confirmed contextual identities, and atomically publishes a verified bundle. Private generations are excluded. See Library [document](../app/modules/library/guidance/documents.md) and [export](../app/modules/library/guidance/site-export.md) contracts. Live preview/export execution and compatibility of the remaining workers with the deployed catalog still need verification.

## Recovery tooling

`scripts/catalog_import.py`, `scripts/catalog_manual_*.py`, `app/catalog/importer.py`, and `app/catalog/cutover.py` describe historical import operations, not current bootstrap. Fresh baseline databases omit the migration-only adapter installers those operations used. Use the historical checkout and persisted manifests for an explicitly planned recovery under [operations](operations.md), with a restore-tested backup and writers paused. Never infer committed state from a client response. Preserve saved index definitions and verify restoration before restarting writers.

[Backup and recovery](postgres-backup-recovery.md) covers portable dumps and isolated restore drills. Any new persisted-data migration or compatibility choice requires owner agreement.
