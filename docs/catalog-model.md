# PostgreSQL catalog contract

## Status

The owner reports migration complete at `20261008_0062`. Retained CLI/workflow handlers use catalog-native reads/writes; credential-backed execution has not established operational readiness. [Architecture](architecture.md) locates owners, [verification](verification.md) defines limits, and [operations](operations.md#database-tools) owns bootstrap/historical recovery. Do not rerun import/retirement to fix backend behavior; inspect deployed revisions/relations/constraints/manifests read-only and repair the affected operation.

## Durable model

| Object | Responsibility |
| --- | --- |
| Publication | Edition/translation/issue; inclusion, bibliography, languages, identifiers, subjects/audiences, credits, one source-work reference, classification, and primary collection |
| Document | Stable MD5 file identity; selection, completeness, restrictions, MIME, accessibility, locations |
| Entity / name / alias / contribution | People/organizations, observed spellings, identity aliases, contextual credit resolution |
| Classification | Parent-child taxonomy, stable classification IDs, generated paths/DDC |
| Collection | Grouping and independent publication switch |
| Preview request / page | Intent, leases, successful generations, selected assets |
| Evidence / proposal / revision | Source/review snapshots, pending changes, transactional audit |

Sync creates one pending publication per new MD5 and preserves existing membership; it never infers edition equivalence. Multiple files may share a publication; cleanup can leave it empty. ISBN equality requires review, not automatic grouping. Inclusion never overrides privacy/completeness; unknown privacy fails closed.

Human confirmation is distinct from active/merged status and AI success. One spelling may belong to multiple identities; linking an alias never resolves all matching mentions. Contributions identify reviewed occurrences. Publisher types derive from publisher mentions, then aliases.

Editable facts live in normalized relations; Schema.org is generated transport. JSONB holds evidence, snapshots, proposals, manifests, and audits. Sparse import evidence plus external backup preserves provenance without duplicating all source payloads.

## Store ownership

PostgreSQL owns domain facts and safety-critical checkpoints; SQLite owns disposable orchestration/provider/retry state under [root rules](../AGENTS.md).

| Workflow | Durable PostgreSQL | Local SQLite |
| --- | --- | --- |
| Personality | Decisions, fingerprints/contracts, identities/conflicts, explicit retries, evidence | Attempts, model exclusions, recovery |
| Non-PDF | Outputs, unsupported decisions, verified MIME/recipes | Attempts/errors/deferrals |
| Cleanup | Plans, reviews, phases, cancellation/completion | Counts, run IDs, errors |
| Previews | Requests, leases, generations/pages | Attempts/errors |
| Metadata | Normalized facts, reviews, evidence/audits | AI attempts/exclusions, quality caches |
| Publisher / collection | Catalog proposals; publisher response checkpoints | Run/progress; collection features rebuilt in memory |

No fallback or dual-write. Retired physical `document`, `metadata`, `classification`, `normalization_canonicals`, `normalization_aliases`, `library_collections`, and `library_collection_items` are adapter views. They store no duplicate rows; legacy upserts require strict `catalog_upsert` because views have no unique indexes.

The baseline omits `library_collection_document_features`, `library_metadata_quality_state`, `library_collection_validation_attempts`, `library_book_previews`, and `publisher_merge_proposals`. Selected previews/publisher proposals use catalog projections. Historical collection proposals/validation and old publisher contracts remain preserved but are unused by current discovery/replay. See flow contracts for eligibility; fresh bootstrap never repeats the completed cutover.

## Relations and constraints

| Relation | Ordered fact / identity |
| --- | --- |
| `catalog_publication_languages` | Publication + position: language |
| `catalog_document_access_modes` | Document + position: accessibility mode |
| `catalog_sufficient_modes` / `catalog_sufficient_mode_items` | Document + group position, ordered child modes |
| `catalog_reference_urls` | Reference + position: URL |
| `catalog_credit_groups` | Publication + role + outer position: group and label |
| `catalog_contributions` | Stable ID, unique nested group position: observed/resolved credit |

Preserve order, repeated values, empty sufficient-mode groups, and absent versus empty URL lists (`urls_present`). `has_metadata` and `metadata_present` differ. Reference authors belong to `catalog_references`; deletion/publication-key changes cascade. Constraints protect credit slots, proposal membership, states, confirmed targets, ages, self-reference/merge status, URL presence, taxonomy/merge cycles, and shared DDC rules.

PostgreSQL 18/UTF-8 triggers own `catalog_name_key`, `catalog_title_key`, and `catalog_identifier_key`. Names use Unicode case folding with `pg_unicode_fast`; collection titles replace runs outside the retained Latin/Tatar-Cyrillic allowlist with spaces and trim. ISBN keys retain only uppercased digits/`X`; other identifier values are unchanged. Derived search keys are not identity/uniqueness claims. Ordered child relations and group-owned labels normalize editable lists; JSONB document semantics and derived keys remain intentional.

Read projections `catalog_publication_metadata`, `catalog_document_metadata`, `catalog_sufficient_mode_metadata`, `catalog_reference_metadata`, and `catalog_contribution_metadata` reconstruct envelopes for Schema.org/adapter views without duplicate domain rows. Scalar projected fields can be updatable; lists and role labels require normalized child writes. Historical helpers using former arrays/labels require review before recovery use.

## Mutations and privacy

Schema owners are Alembic frozen SQL and `app/catalog/schema.py`; Python metadata alone is incomplete. Shared commands live in `app/catalog/`. Mutations check source/reviewed revisions and write audits transactionally; stale snapshots raise `CatalogConflict`. Metadata needs publication/document revisions and preserves protected fields and confirmed contributions. Protected publication changes become review proposals; file/privacy/collection conflicts require explicit resolution. No general admin or metadata-proposal review CLI exists.

Preview leases/claim tokens are durable; expired claims recover and failed regeneration retains prior success. Restricted outputs require private storage/authenticated delivery. Restricted files lose public preview eligibility; old object deletion remains guarded Maintenance work. Public automatic previews and static export follow [document](../app/modules/library/guidance/documents.md) and [bundle](../app/modules/library/guidance/site-export.md) contracts.

Any new persisted-data migration or compatibility choice requires owner agreement.
