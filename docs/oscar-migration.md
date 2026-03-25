# Oscar Flow Migration Plan

Status: draft checklist for controlled migration in small steps.

## Scope (Current Agreement)

- Flow name: `oscar`
- Source repository: `~/projects/oscar-corpus-extractor`
- Runtime state store: PostgreSQL (`MANZARA_DATABASE_URL`), schema `monocorpus`
- Artifacts root: `~/.manzara` (Oscar under `~/.manzara/oscar`)
- Preferred download strategy: `download_ranges` only
- Stage order per snapshot:
  1. `resolve_offsets_local`
  2. `download_ranges`
  3. `export_parquet` with `1024 MB` part size
- Snapshot policy: process next unprocessed snapshot each run
- Out of scope for now: embedding source repo code and production stage logic

## Step Plan

- [x] Step 1: Requirements freeze and migration checklist (this doc)
- [x] Step 2: Flow skeleton in Manzara (panel + tasks + lightweight runners)
- [x] Step 3: PostgreSQL state model for snapshot queue/progress
- [x] Step 4: Implement `resolve_offsets_local`
- [x] Step 5: Implement `download_ranges`
- [x] Step 6: Implement `export_parquet` (`1024 MB` parts)
- [x] Step 7: Workflow automation for next-unprocessed pipeline

## Intended Runtime Data Model (for Step 3)

- `oscar_snapshots`:
  - snapshot id/key
  - source metadata (date/version/path)
  - lifecycle state (`pending`, `processing`, `completed`, `failed`)
  - timestamps
- `oscar_snapshot_stages`:
  - snapshot id/key
  - stage name (`resolve_offsets_local`, `download_ranges`, `export_parquet`)
  - stage status
  - run linkage (`run_id`)
  - timestamps/error text

## Acceptance Criteria by Step

- Step 2 acceptance:
  - Oscar panel is visible on dashboard.
  - Oscar tasks appear in Tasks page and are runnable placeholders.
  - Placeholder tasks log clear `not_implemented_yet` output and exit successfully.
- Step 3+ acceptance:
  - No SQLite runtime paths.
  - Snapshot progression is resumable and auditable from DB.
  - Logs and SSE state transitions remain consistent with existing task model.
