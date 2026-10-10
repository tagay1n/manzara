# Manzara

Tatar-language content operations through an inline terminal CLI and GitHub Actions. Durable catalog data lives in PostgreSQL; local orchestration uses SQLite. Web pages and HTTP APIs are retired.

## Setup and launch

Use Python 3.12+ and an initialized PostgreSQL catalog. Copy the masked structure of [config.example.yaml](config.example.yaml) to gitignored `config.yaml` and fill the required settings. `MANZARA_CONFIG_PATH` selects another explicit YAML file; environment variables do not override its values. [Operations](docs/operations.md#runtime-configuration) covers configuration and separate database bootstrap.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app
```

Execution requires a terminal; `--help` needs no configuration. Launch restores the last selected task, including tasks selected without running them. `--task TASK_ID` overrides and updates that selection; without a saved registered task, YAML `terminal.initial_task` supplies the default. `--limit N` sets the candidate limit; limits and other run options remain session-specific. Launch always remains idle until `/run`.

| Interactive task ID | Contract / controls |
| --- | --- |
| `library.normalize_personalities` | [Personality normalization](docs/personality-normalization.md) |
| `library.extract_non_pdf` | [Documents](app/modules/library/guidance/documents.md#extraction-and-publication); `--per-mime-limit`, repeated `--only-md5`, `--retry-known-failures` |
| `library.generate_book_previews` | [Previews](app/modules/library/guidance/documents.md#sources-and-previews); repeated `--only-md5`, `--retry-known-failures` |
| `library.site_export` | [Static export](app/modules/library/guidance/site-export.md); complete inventory |
| `library.suggest_publisher_merges` | [Publisher proposals](app/modules/library/guidance/publisher-merges.md); complete inventory, subscription-authenticated Codex |
| `library.collection_detect` | [Collection proposals](app/modules/library/guidance/collections.md); complete inventory, deterministic |
| `library.metadata_extract` | [Metadata](app/modules/library/guidance/metadata.md); missing metadata only, one SQL query per batch of up to 200 publications; limit counts publications |
| `maintenance.monocorpus_meta_evaluate` | [Metadata](app/modules/library/guidance/metadata.md); limit counts publications |

Complete-inventory tasks reject candidate limits. Publisher and collection tasks generate proposals; review/apply commands are unavailable.

Daily cleanup preparation and Yandex Sync use `python scripts/run_daily_maintenance.py`. Backblaze transfer uses its separate GitHub workflow. Neither is an interactive task. See [scheduled operations](docs/operations.md#scheduled-sync-and-cleanup) and [cleanup review commands](docs/document-cleanup.md).

## Controls and lifecycle

| Command | Behavior |
| --- | --- |
| `/task` | Search/select a task without starting it |
| `/settings` | Edit limit; preserve cohort/retry options |
| `/run` | Start/resume selected task |
| `/stop` | Stop safely and remain open |
| `/history` | Inspect selected task's latest 20 runs |
| `/summary` | Show foreground run or latest saved summary |
| `/help` | Show commands and keyboard guidance |
| `/quit` | Stop safely, drain output, exit |

Type `/` for completion; arrows navigate, Enter chooses, Tab completes, Esc dismisses. Task/history pickers support search and scrolling; long labels wrap. Settings use Tab for focus, Enter/Ctrl-S to save, Esc to cancel. Outside pickers, Up/Down recall commands and restore unfinished input after the newest entry. Valid slash commands survive restarts, retaining the latest 1,000 entries and skipping consecutive duplicates. Unrecognized input stays in current-session recall only. Ordinary text, including `q`, is never executed as a shell command.

One foreground run owns the CLI through worker finalization and output drain. Further starts and task/settings changes are rejected; history browsing preserves foreground identity. Activity animates through discovery, provider waits, stopping, and finalization; counters indicate progress. Runtime-read failure marks progress unavailable. Summaries distinguish completed, stopped, deferred, and failed outcomes. Finalized runs ring the terminal bell once; `PROMPT_TOOLKIT_BELL=false` disables it.

Ctrl-C during work requests safe stop. At idle it clears input, or exits when input is empty. A second Ctrl-C while stopping/exiting forces exit 130 and restores input mode without waiting for output; unfinished checkpoints and final summaries may be skipped. The next launch recovers interrupted runs. Slow output applies bounded backpressure while controls remain responsive; output failure requests safe shutdown and exits 1. Normal shutdown drains output.

One CLI session owns each local runtime store. Startup initializes SQLite and checks PostgreSQL read-only; it never applies migrations. [Local reset policy](docs/operations.md#local-runtime-state) governs older SQLite schemas. Tasks run sequentially, log to redacted stdout, and save JSON results under the configured artifact root for summaries/history. Provider I/O timeouts do not bound total streaming duration.

## Documentation

- [Repository rules](AGENTS.md), [implementation map](docs/architecture.md), [catalog contract](docs/catalog-model.md).
- [Operations](docs/operations.md), [backup/recovery](docs/postgres-backup-recovery.md), [Gemini runtime](docs/gemini-runtime.md).
- [Verification policy and limits](docs/verification.md), [active work](TODO.md).
