# Library metadata processing

## CLI and catalog ownership

`library.metadata_extract` fills missing metadata only (`metadata_present = false`); existing metadata is left alone regardless of quality. At each loop iteration, one PostgreSQL SELECT chooses up to 200 unmerged publications with eligible evidence and no pending task metadata review, then aggregates normalized metadata and ordered source details for that batch. Independent child aggregates avoid multiplying rows across unrelated relations. Python renders Schema.org from the returned envelope and checks local retry state; it never invokes the single-document reader during discovery.

Release the connection before processing each batch sequentially. Continue from its last publication ID even when local deferrals exclude every item, so a run never cycles on failed/deferred publications. Stop when the candidate query returns no rows, the eligible-publication limit is reached, safe stop is requested, or a fatal/global provider outcome prevents work. Each query observes a fresh statement snapshot; mutations recheck source/publication revisions and metadata. No full-run candidate inventory or quality cache is built. Progress totals count eligible publications fetched so far; `scanned` counts queried publications, and `inventory_exhausted` identifies an empty-query termination rather than a limit/stop/abort.

`maintenance.monocorpus_meta_evaluate` retains its fixed ID-ordered publication inventory. For both tasks, `--limit` counts publications. Source/per-MIME/retry flags are rejected. Retained entry modules select these CLI tasks; standalone batch/dry-run/excerpt arguments are absent. Owners: [navigation](navigation.md), [evaluation lookup](evaluation-navigation.md).

Prefer selected, then MD5-ordered complete unrestricted documents with verified primary storage and usable content/PDF/DjVu evidence. Try another file after preparation failure; provider content rejection ends the publication turn. Extraction discovers only publications with usable sources; evaluation defers publications without them. Privacy, incompleteness, and filenames never automatically exclude publications; evaluation retains configured Tatar filtering and uses content/metadata applicability.

Read normalized publication/document/name/contribution/location relations, never write adapter views. Inclusion/classification belong to publications; privacy/accessibility/extraction/storage belong to documents. Recheck revisions, membership, eligibility, and metadata/upstream snapshot before short mutations. Conflicts preserve model attempts for fresh inventory; provider calls hold no PostgreSQL connection.

Honor field protections. Preserve IDs/reviewed resolution for unchanged contributions; removing/rewriting confirmed occurrences enters review. Generated names/contributions stay unconfirmed and source-script spellings remain AI input. Normalize languages, credit groups, accessibility, sufficient modes, reference URLs; preserve order, empty groups, absent/empty URLs, and non-ISBN identifiers.

Extraction fills only absent metadata with validated output and records evidence-document method. It resets earlier unprotected automatic evaluation to pending inclusion/empty classification-method; protected conflicts become proposals. Evaluation atomically commits permitted patches/inclusion/method/taxonomy/evidence/audits. Reuse taxonomy IDs; new paths are pending Gemini classifications. Validate the protected merged envelope. Pending task metadata proposals pause both tasks until reviewed.

## Retry state and output

SQLite owns attempts/deferrals/exclusions/quality caches under `library.metadata_extract.catalog.v1`, `library.metadata_evaluate.catalog.v1`, and `library.metadata_quality.catalog.v1`. Checkpoint identity includes publication ID, evidence hash, and prompt contract. Historical MD5 checkpoints/failure files are untouched and unused. Changed evidence/prompt/model pools reopen eligible work; resume untried models and clear checkpoints only after durable results/proposals commit.

Use [shared Gemini](../../../../docs/gemini-runtime.md). Content/schema failures consume model attempts; quota/service/storage/database/stop outcomes do not content-exclude. Exhausted publications defer while others continue; global provider unavailability preserves untouched work. Authentication/configuration/unexpected implementation failures fail the run. Cancellation/finalization follow shared runtime rules.

Run workspaces retain `items.json` and corruption-plan artifacts. Summaries link publication outcomes, source MD5s, review IDs, and per-model attempts/successes; common stdout/progress/artifact policy lives in root/runtime rules.

Extraction logs validated model-returned Schema.org as indented JSON, identified by publication ID, source MD5, and model, through shared redacted stdout logging before the guarded catalog save.

## Extraction evidence and validation

- Populate MD5-verified cache misses only from primary Backblaze; never mutate storage URLs/upload metadata ZIPs.
- Preserve extraction prompt, Schema.org contract, PDF edge slicing, and normalization. Sanitize database-owned `library_upstream_metadata` in text/visual requests.
- Absent title requires another bibliographic/content signal and valid `inLanguage`. Never overwrite existing metadata or erase language with null.
- Strict JSON-LD validation precedes writes. Genre/audience/classification/role facets are English; descriptions follow `inLanguage`. Never truncate fractional page/age counts.
- Reject mixed scripts only at two-to-one competing dominance; treat `Ьь` as Yanalif and preserve `tt-Latn-x-zaman-alif`.
- Deterministic PDF open/page-tree/page-read failure may create guarded `corrupted` plans; active cleanup excludes sources. Password protection/service/storage failures defer operationally; Maintenance executes cleanup.
- DjVu is visual-only: verified sources, unique first/last three pages rendered to PDF, reduced size below Gemini limits. Ignore extracted content; missing tools/timeouts are operational failures.

## Evaluation evidence and validation

- Preserve valid positive/negative decisions. Select missing inclusion, included without classification, or excluded with classification. Missing metadata awaits extraction; existing unusable metadata defers evaluation as `unusable_metadata` and requires explicit correction.
- Use database upstream evidence, never remote fetching/Yandex source fallback.
- Require explicit boolean/reason; applicable responses also require normalized DDC/English category path. Validate the merged envelope, not just patches.
- DjVu uses first/last two rendered pages; PDF uses that count when text is unavailable. Unpreparable evidence defers.
- Report publication/skipped/terminal/deferred counts and per-model attempts/successes; log publication ID, source MD5, resolved model before requests.
