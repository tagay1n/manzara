# Manzara

Tatar-language content operations through an inline terminal CLI and GitHub Actions. Durable catalog data lives in PostgreSQL; local orchestration uses SQLite. Web pages and HTTP APIs are retired.

## Setup and launch

Use Python 3.12+ and an initialized PostgreSQL catalog. Copy the masked structure of [config.example.yaml](config.example.yaml) to gitignored `config.yaml` and fill the required settings. `MANZARA_CONFIG_PATH` selects another explicit YAML file; environment variables do not override its values. [Operations](docs/operations.md#runtime-configuration) covers configuration and separate database bootstrap.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app
```

Execution requires a terminal; `--help` needs no configuration. Launch restores the last selected task, including tasks selected without running them. `--task TASK_ID` overrides and updates that selection; without a saved registered task, YAML `terminal.initial_task` supplies the default. YAML `terminal.task_limits` supplies per-task candidate limits; omitted/null entries mean unlimited. `--limit N` overrides them for this launch; `/settings` can save the selected task's limit as a new default. Cohort/retry flags remain session-specific. Launch always remains idle until `/run`.

| Interactive task ID | Contract / controls |
| --- | --- |
| `library.normalize_personalities` | [Personality normalization](docs/personality-normalization.md) |
| `library.extract_non_pdf` | [Documents](app/modules/library/guidance/documents.md#extraction-and-publication); `--per-mime-limit`, repeated `--only-md5`, `--retry-known-failures` |
| `library.generate_book_previews` | [Previews](app/modules/library/guidance/documents.md#sources-and-previews); repeated `--only-md5`, `--retry-known-failures` |
| `library.site_export` | [Static export](app/modules/library/guidance/site-export.md); complete inventory |
| `library.suggest_publisher_merges` | [Publisher proposals](app/modules/library/guidance/publisher-merges.md); complete inventory, subscription-authenticated Codex |
| `library.collection_detect` | [Collection proposals](app/modules/library/guidance/collections.md); complete inventory, deterministic |
| `library.metadata_extract` | [Metadata](app/modules/library/guidance/metadata.md); missing metadata only, one SQL query per batch of up to 200 publications; limit counts publications |
| `maintenance.monocorpus_meta_evaluate` | [Metadata](app/modules/library/guidance/metadata.md); pending file inclusion/classification, one SQL query per batch of up to 200 files; limit counts files |

Complete-inventory tasks reject candidate limits. Publisher and collection tasks generate proposals; review/apply commands are unavailable.

Daily cleanup preparation and Yandex Sync use `python scripts/run_daily_maintenance.py`. Backblaze transfer uses its separate GitHub workflow. Neither is an interactive task. See [scheduled operations](docs/operations.md#scheduled-sync-and-cleanup) and [cleanup review commands](docs/document-cleanup.md).

## Controls and lifecycle

| Command | Behavior |
| --- | --- |
| `/task` | Search/select a task without starting it |
| `/settings` | Save selected task's defaults to private YAML; Extract metadata also offers response timeout and first/last page counts; preserve cohort/retry options |
| `/run` | Start/resume selected task |
| `/stop` | Stop safely and remain open |
| `/history` | Inspect selected task's latest 20 runs |
| `/summary` | Show foreground run or latest saved summary |
| `/gemini` | Show configured key/account counts and exhaustion counts per model |
| `/reset` | Clear exhaustion for configured Gemini keys/models, including during a run |
| `/help` | Show commands and keyboard guidance |
| `/quit` | Stop safely, drain output, exit |

Type `/` for completion; arrows navigate, Enter chooses, Tab completes, Esc dismisses. Task/history pickers support search and scrolling; long labels wrap. Settings use Tab/Shift-Tab to move, Up/Down to adjust numbers, Enter to advance or activate a button, Ctrl-S to save, and Esc to cancel. The focused field shows units/ranges/help; invalid input keeps the form open and focuses the field. Outside pickers, Up/Down recall commands and restore unfinished input after the newest entry. Valid slash commands survive restarts, retaining the latest 1,000 entries and skipping consecutive duplicates. Unrecognized input stays in current-session recall only. Ordinary text, including `q`, is never executed as a shell command.

`/settings` belongs to the selected task. Save defaults writes the selected private YAML (`config.yaml` or `MANZARA_CONFIG_PATH`), preserves comments and unrelated values, and survives restarts. Cancel changes nothing. Saving requires an idle task; failed writes leave the form open without applying options, and external edits require reopening the form. Candidate limits are saved under `terminal.task_limits`; complete-inventory tasks offer no candidate limit. Extract metadata edits YAML `metadata.response_timeout_seconds`, `first_pages`, and `last_pages` directly. Response timeout is an integer from 1 to 900 seconds, covering generation start through the complete streamed Gemini response; preparation, uploads, provider waits, cleanup, and catalog saves are outside it. A timeout defers the publication for retry without consuming a content attempt. First/last counts are nonnegative integers with at least one page selected; visual PDF/DjVu evidence uses unique pages in source order. Text evidence retains its separate YAML character limit. Evaluation has separate evidence settings. Run summaries retain the effective extraction settings.

Gemini exhaustion persists across quota-day rollover and restarts until `/reset`; requests have no midnight blackout. Reset preserves temporary cooldowns, model pauses, request spacing, leases, counters, and error history. It does not restart a finished/deferred task; use `/run` to resume. Provider errors after a reset can exhaust keys again. Both commands use local SQLite state without calling Gemini.

One foreground run owns the CLI through worker finalization and output drain. Further starts and task/settings changes are rejected; history browsing preserves foreground identity. Activity animates through discovery, provider waits, stopping, and finalization; counters indicate progress. Runtime-read failure marks progress unavailable. Summaries distinguish completed, stopped, deferred, and failed outcomes. Successful finalized runs ring the terminal bell (`\a`) once; failures ring twice, spaced apart. Stopped/deferred runs stay silent. `PROMPT_TOOLKIT_BELL=false` disables CLI bells; audibility depends on terminal settings. Batch stages use the same patterns when stdout is a terminal and emit no bells into redirected or workflow output.

Ctrl-C during work requests safe stop. At idle it clears input, or exits when input is empty. A second Ctrl-C while stopping/exiting forces exit 130 and restores input mode without waiting for output; unfinished checkpoints and final summaries may be skipped. The next launch recovers interrupted runs. Slow output applies bounded backpressure while controls remain responsive; output failure requests safe shutdown and exits 1. Normal shutdown drains output.

One CLI session owns each local runtime store. Interactive startup applies pending PostgreSQL migrations and waits for completion before enabling tasks, then initializes SQLite. Migration failures keep tasks unavailable. [Database tools](docs/operations.md#database-tools) describes migration ownership and recovery; [local reset policy](docs/operations.md#local-runtime-state) governs older SQLite schemas. Tasks run sequentially, log to redacted stdout, and save JSON results under the configured artifact root for summaries/history. Provider I/O timeouts do not bound total streaming duration.

## Documentation

- [Repository rules](AGENTS.md), [implementation map](docs/architecture.md), [catalog contract](docs/catalog-model.md), [database reference for the administration service](docs/database-reference.md).
- [Operations](docs/operations.md), [backup/recovery](docs/postgres-backup-recovery.md), [Gemini runtime](docs/gemini-runtime.md).
- [Verification policy and limits](docs/verification.md), [active work](TODO.md).
