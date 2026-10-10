# Library metadata processing

## CLI and catalog ownership

`library.metadata_extract` and `maintenance.monocorpus_meta_evaluate` run through the interactive CLI. Both accept workers (default one) and a publication limit. Source-cohort, per-MIME, and explicit retry flags belong to other tasks and are rejected here. Retained script entry points select the same CLI tasks; the old standalone evaluation batch-size/dry-run/excerpt arguments are removed. Selection never starts processing.

Both tasks process a fixed publication inventory in ID order. Prefer selected files, then MD5 order, among complete, unrestricted documents with a verified primary-storage checkpoint and usable content/PDF/DjVu evidence. Try another eligible file after source-preparation failure; a provider content rejection ends the publication's current turn. Publications without usable sources remain deferred. Restrictions, incompleteness, and filename patterns never automatically exclude a publication. Evaluation retains configured Tatar-language filtering and decides applicability from content and metadata.

Read normalized publication/document/name/contribution/storage relations; never write legacy document/metadata views. Publication inclusion and classification are shared by all its documents. File restrictions, accessibility, extraction markers, and storage remain document-owned. Recheck publication/document/storage revisions, membership, source eligibility, and the metadata/upstream evidence snapshot before each short mutation transaction. Conflicts leave the model attempt available for a fresh inventory. Provider calls do not hold PostgreSQL connections.

Automated writes honor field protections. Unchanged contributions retain their IDs and reviewed resolution. Changes removing or rewriting confirmed occurrences enter metadata review; generated names become unconfirmed contributions without entity/alias confirmation. Read observed spellings as AI input and preserve names in their source script. Catalog metadata commands update normalized language, credit-group, accessibility, sufficient-mode, and reference-URL relations. Retain ordered values, empty sufficient-mode groups, absent versus empty reference URL lists, and unrelated non-ISBN identifiers.

Successful extraction replaces unusable metadata only with a validated result and records the evidence document's extraction method. It reopens an earlier unprotected automatic evaluation by resetting inclusion to pending and clearing classification/method. Protected decisions remain intact; conflicting changes become proposals. Evaluation commits permitted metadata patches, publication inclusion/method, normalized taxonomy, evidence, and revision audits atomically. Reuse existing taxonomy nodes/classification IDs; new paths are pending Gemini classifications. Validate the resulting envelope after field protections. Pending task-generated metadata proposals pause both tasks for that publication until explicitly reviewed, avoiding repeated proposals.

## Retry state and output

AI attempts, operational deferrals, terminal exclusions, and metadata-quality caches use only local SQLite. Fresh namespaces are `library.metadata_extract.catalog.v1`, `library.metadata_evaluate.catalog.v1`, and `library.metadata_quality.catalog.v1`. AI checkpoint identities include publication ID and an evidence-snapshot hash; prompt versions remain part of the checkpoint contract. Old MD5 checkpoints and evaluation failure files remain untouched and are never imported or consulted. Changed evidence/prompt/model pools reopen applicable work; resume with untried models and clear checkpoints only after durable results or review proposals commit.

Use the shared configured Gemini runtime and model pool. Content/schema failures consume a model attempt; quota, service, storage, PostgreSQL availability, and stop conditions do not create content exclusions. Exhausted publications remain deferred while other publications continue. Global provider unavailability ends the queue with resumable untouched work. Authentication/configuration and unexpected implementation failures fail the run instead of looping. Workers share explicit cancellation and inherited log context, then finalize before summaries are published.

Logs stream to terminal scrollback without `.log` files, automatic prompt dumps, lifecycle events, or database log/progress events. Retain local run state, heartbeat, coalesced progress, and explicit `task.artifact` events. Dedicated `items.json` and corruption-plan artifacts live in the run workspace under `~/.manzara` or `MANZARA_ARTIFACTS_ROOT`; final summaries link item results and preserve publication counts, source MD5s, review proposal IDs, and per-model attempts/successes. This work has static inspection only; live execution and tests require explicit owner authorization under [verification](../../../../docs/verification.md).

## Extraction evidence and validation

- Reuse the MD5-verified source cache; populate misses only from configured Backblaze primary storage. Never mutate storage URLs or upload metadata ZIPs.
- Preserve the adopted extraction prompt, Schema.org contract, PDF edge-page slicing, and normalization. Read supporting upstream evidence only from `library_upstream_metadata` and sanitize it in both text and visual requests.
- Metadata may omit a genuinely absent title, but requires another bibliographic/content signal and valid `inLanguage`. Never overwrite usable publication metadata during ordinary extraction or erase language with null.
- Validate the strict JSON-LD contract before every write. Canonical discovery facets (`genre`, audience types, classification paths, and role names) are English; descriptions follow `inLanguage`. Do not truncate fractional page/age counts.
- Validate description scripts conservatively: require two-to-one competing-script dominance before rejecting mixed text, treat `Ьь` as Yanalif letters, and preserve `tt-Latn-x-zaman-alif`.
- Deterministic PDF open/page-tree/page-read failures may create a guarded `corrupted` move plan; active cleanup plans exclude the source. Password protection and service/storage failures are operational deferrals. Cleanup execution remains Maintenance-owned.
- DjVu is visual-only: use verified cached/primary sources and unique first/last three pages rendered into PDF. Reduce rendering size to stay below Gemini limits. Ignore extracted content even when present; missing tools and timeouts are operational failures.

## Evaluation evidence and validation

- Preserve valid positive and negative evaluations. Select missing decisions, included publications without classification, or excluded publications retaining classification. Unusable metadata waits for extraction.
- Use database-owned upstream metadata; never fetch remote upstream evidence or fall back to Yandex source URLs.
- Require an explicit boolean decision and concise reason; applicable responses also need normalized DDC and an English category path. Validate the fully merged envelope, not only a patch.
- DjVu always uses rendered first/last two pages. PDF evaluation uses the same page count when text evidence is unavailable. Defer sources whose evidence cannot be prepared.
- Publish publication counts, skips, terminal/deferred outcomes, and per-model attempts/successes. Log publication ID, source MD5, and resolved model before requests.
