# Manzara

Manzara runs Tatar-language content workflows through an inline terminal CLI and GitHub workflow runners. Alembic provides separate schema bootstrap. Web pages, HTTP APIs, and SSE transport are retired.

## Current status

**Normalize personalities**, **Extract non-PDF**, **Generate book previews**, **Export static library**, **Cluster publishers**, **Discover collections**, **Extract metadata**, and **Evaluate metadata** are enabled interactive CLI tasks. Publisher clustering generates proposals using subscription-authenticated Codex; review/apply remains deferred. Normalization uses the shared Gemini runtime; extraction uses local document converters and verified Backblaze sources. Cleanup preparation and Sync run together through standalone daily maintenance; cleanup review commands remain in the CLI. These workflows use the normalized PostgreSQL catalog; processing tasks retain resumable checkpoints, while export rebuilds a consistent snapshot. Backblaze document transfer has a separate automatic/manual GitHub workflow and is absent from the CLI. Operational readiness must be assessed for each execution path; static inspection alone does not establish it.

## Setup and launch

Use Python 3.10+ and PostgreSQL. Install dependencies in the existing environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app
```

Copy the masked structure of `config.example.yaml` to gitignored `config.yaml` and replace masked values. `MANZARA_CONFIG_PATH` can select another explicit YAML file. Every operational setting comes from that file; missing settings fail and environment variables do not override values. Actions-only defaults live in `.github/config/` and workflow setup combines them with existing secrets into private YAML. Never load the example at runtime. [Runtime configuration](docs/operations.md#runtime-configuration) covers policy fields and workflow provisioning.

Optional `--limit N` and `--task TASK_ID` select next-run settings and the initial interactive task. Launching never starts a task automatically. Interactive execution requires a terminal; `--help` works without configuration. Backblaze upload is available only through its [GitHub workflow](docs/operations.md#backblaze-document-transfer).

Select non-PDF extraction with `.venv/bin/python -m app --task library.extract_non_pdf --per-mime-limit 1`, then enter `/run`. It runs sequentially with one worker. [Document processing](app/modules/library/guidance/documents.md#extraction-and-publication) owns source eligibility, converter requirements, cohort/retry controls, and retained output behavior.

Select book previews with `.venv/bin/python -m app --task library.generate_book_previews --limit 10`, then enter `/run`. It uses one worker and supports `--only-md5` and `--retry-known-failures`. [Preview processing](app/modules/library/guidance/documents.md#sources-and-previews) owns eligibility, page detection, recovery, and storage requirements.

Select static export with `.venv/bin/python -m app --task library.site_export`, then enter `/run`. It requires one worker and the complete inventory. The versioned bundle is published under `~/.manzara/durable/library/site-exports/` or the configured artifact root. [Static export](app/modules/library/guidance/site-export.md) owns eligibility, snapshot consistency, bundle verification, and publication behavior.

Select publisher clustering with `.venv/bin/python -m app --task library.suggest_publisher_merges`, then enter `/run`. It requires one worker and the complete inventory, so candidate limits are rejected. `/summary` and `/history` show saved counts and artifact/workspace paths. [Publisher analysis](app/modules/library/guidance/publisher-merges.md) owns grouping, configuration, checkpoints, and output behavior.

Select collection discovery with `.venv/bin/python -m app --task library.collection_detect`, then enter `/run`. It requires one worker and the complete inventory and generates deterministic publication-based proposals without AI or membership/inclusion changes. Logs stream to the terminal without a log file or lifecycle events; local run tracking and the final structured artifact remain available. [Collection discovery](app/modules/library/guidance/collections.md) owns the contract.

Select metadata extraction with `.venv/bin/python -m app --task library.metadata_extract`, or evaluation with `--task maintenance.monocorpus_meta_evaluate`, then enter `/run`. Both process publications sequentially and support a publication limit. All tasks use stdout-only logs, emit no events, and retain structured result artifacts. [Metadata processing](app/modules/library/guidance/metadata.md) owns source selection, protected writes, and retry behavior.

Run daily maintenance without a terminal using `python scripts/run_daily_maintenance.py`. It prepares cleanup plans/reviews, then executes persisted cleanup and Yandex Sync with one worker and no limit. Sync and Cleanup plan are absent from the interactive task list. [Operations](docs/operations.md#scheduled-sync-and-cleanup) owns the daily schedule, Actions secrets, retention, and failure behavior; [cleanup review](docs/document-cleanup.md) explains explicit ISBN decisions.

## Controls and lifecycle

The CLI launches idle with a compact activity row and an inline prompt. Type `/` for a filtered command picker; arrows navigate, Enter chooses, Tab completes, and Esc dismisses. Ordinary text receives a command hint; it is never executed as a shell command.

Outside pickers, Up recalls older submitted commands and Down moves toward newer ones, restoring your unfinished input after the newest command. Recalled commands stay in the prompt until you press Enter; editing one resumes command completion. History includes commands chosen from the picker, skips empty input and consecutive duplicates, and lasts for the current CLI session. Press Esc to dismiss an open command picker before browsing command history. `/history` remains the task run picker.

| Command | Behavior |
| --- | --- |
| `/task` | Search and select a task. Selection never starts work. |
| `/settings` | Edit candidate limit with inline validation, Save, and Cancel. Tab changes focus; Enter/Ctrl-S saves; Esc cancels. Cohort/retry options are preserved. |
| `/run` | Start/resume the selected task using current options. |
| `/stop` | Request safe stop and keep Manzara open. |
| `/history` | Search the selected task's latest 20 runs and print a saved summary. |
| `/summary` | Print the foreground snapshot, or the selected task's latest saved summary at idle. |
| `/help` | Print command and keyboard guidance. |
| `/quit` | Stop safely, drain output, and exit. |

One foreground run owns the CLI from the start request through worker finalization and output drain. Additional starts are rejected, and task selection/settings stay locked. Browsing history leaves foreground activity unchanged. Activity animates during discovery, processing, provider waits, stopping, and finalization; the spinner indicates activity, while meaningful counters indicate advancement. Provider waits come directly from the run status. Runtime-read failures visibly mark progress unavailable. Completion distinguishes completed, stopped, deferred, and failed outcomes.

When a run finishes, the CLI rings the terminal bell once after worker finalization and final output drain, including failed, deferred, and safely stopped runs. The summary shows the outcome. Sound depends on your terminal's audible-bell settings; set `PROMPT_TOOLKIT_BELL=false` to disable it. Browsing saved summaries does not ring the bell.

Ctrl-C during work requests safe stop and returns to idle after finalization. At idle it clears nonempty input, or exits if input is empty. Press Ctrl-C again while stopping/exiting to force process exit with code 130, including during startup or finalization. Force exit restores terminal input mode without waiting for output; cursor/style restoration is best effort when the terminal cannot accept writes. Interrupted runs are recovered on the next launch and resume from persisted checkpoints. Force exit can skip final output, summaries, and unfinished checkpoint writes. Provider requests have a 60-second I/O timeout, not a hard total-duration limit. A bare `q` is ordinary input.

Renderer updates and transcript messages share a nonblocking terminal writer. Input stays attached while output is slow; redraws are coalesced and log producers receive backpressure until the terminal catches up. Output failure requests safe shutdown and returns exit code 1. Normal shutdown drains pending output before returning.

Reopening preserves compatible completed decisions and resumes eligible work. One CLI session owns each local runtime store; a second session reports the conflict.

CLI and maintenance startup initialize local SQLite and recover interrupted local runs. Python task registrations own task titles, grouping, inventory requirements, and handlers. Older local runtime databases are recreated once for schema version 8 under the owner-approved [fresh-state policy](docs/operations.md#local-runtime-state). They do **not** apply PostgreSQL migrations. All interactive tasks, daily maintenance, and Backblaze transfer check the catalog read-only before processing; incompatible schemas must be migrated separately.

Operational settings, including database schema/pool, paths, Gemini retry/pacing, conversion deadlines, previews, and integration targets, are explicit in [config.example.yaml](config.example.yaml). `MANZARA_CONFIG_PATH` selects the file; it supplies no setting values. Task selection, cohort limits, and explicit retry flags remain per-run controls.

Every enabled task shares the process's bounded PostgreSQL engine. Task definitions are code-owned. Runs, Gemini coordination, and AI retry exclusions remain in local SQLite; domain data and safety-critical checkpoints remain in PostgreSQL.

Every task processes items sequentially with one worker. Necessary support threads keep terminal controls and leases responsive. Interactive and scheduled logs use the same redacted stdout formatter; no task creates log files or emits events. Result artifacts are separate JSON files under `workspaces/task-runs/<task_id>/run-<run_id>/`, linked from local run summaries. Existing files remain on disk. The upgrade resets local runtime state according to the [approved policy](docs/operations.md#local-runtime-state).

Gemini models and account/project-grouped keys come from local configuration, with no model default. See the [Gemini contract](docs/gemini-runtime.md).

## Guidance

- [Repository rules](AGENTS.md) and [architecture](docs/architecture.md).
- [Personality normalization](docs/personality-normalization.md), [cleanup planning and review](docs/document-cleanup.md), and [catalog model](docs/catalog-model.md).
- [Verification limits](docs/verification.md) and [active work](TODO.md).
- [Operations](docs/operations.md) and [PostgreSQL recovery](docs/postgres-backup-recovery.md).
