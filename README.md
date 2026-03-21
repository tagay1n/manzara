# manzara

Manzara is a cloud-console style dashboard for managing long-running content workflows.

This first MVP slice includes:
- Shayan panel with icon-only task controls.
- Maintenance panel with operations task controls.
- SQLite-backed task/runs/logs/events storage.
- Start -> graceful stop -> force stop toggles.
- Header-level two-step stop-all control.
- Live updates via SSE (`/api/events/stream`).
- Weekly workflow scheduler (`scan -> conditional download`).

## Implemented MVP Scope

- Backend: FastAPI + SQLite
- Panel: `Shayan`
- Task: `scan for changes`
- Task: `download new`
- Task: `maintenance monocorpus sync`
- Workflow: `Weekly Sync` (`shayan.weekly_sync`)
- Workflow: `Library` (`library.meta_evaluate`)
- Schedule: weekly, overlap skip, catch-up once after downtime
- Run history + live logs
- Basic metrics from Shayan artifacts (`status.json`, `last-main-run-summary.json`)

## Requirements

- Python 3.10+
- Access to local downloader repo (default: `/home/tans1q/projects/shayan-video-downloader`)

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Test Setup

```bash
.venv/bin/pip install -r requirements-dev.txt
```

Run tests:

```bash
.venv/bin/python -m pytest -q
```

## Run

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
```

Open:
- `http://127.0.0.1:8080/dashboard`
- `http://127.0.0.1:8080/schedules`

## Configuration

Environment variables:
- `MANZARA_DB_PATH` (default: `data/manzara.db`)
- `MANZARA_ENABLE_SCHEDULER` (default: `1`; set `0` to disable scheduled triggers)
- `SHAYAN_REPO_PATH` (default: `/home/tans1q/projects/shayan-video-downloader`)
- `SHAYAN_OUTPUT_PATH` (default: `/home/tans1q/video-archive`)
- `MONOCORPUS_REPO_PATH` (default: `/home/tans1q/projects/monocorpus`)

Monocorpus embedded runtimes (`sync` and `meta evaluate`) read shared YAML config from repo root:
- Local real config (gitignored): `./config.yaml` (or `./config.local.yaml`)
- Safe committed copy: `./config.example.yaml`

Lookup order for embedded runtimes:
1. `MANZARA_CONFIG_PATH` (if set)
2. `./config.local.yaml`
3. `./config.yaml`
4. `./config.example.yaml` (for shape/reference only; should stay masked)

Rule: when config structure changes, update `config.example.yaml` with the same keys and masked secret values.

## API Endpoints

- `GET /api/health`
- `GET /api/dashboard`
- `GET /api/schedules`
- `POST /api/tasks/{task_id}/toggle`
- `POST /api/workflows/{workflow_id}/run`
- `GET /api/workflows/{workflow_id}`
- `PATCH /api/schedules/{schedule_id}`
- `POST /api/system/stop-all`
- `GET /api/runs/{run_id}/logs`
- `GET /api/events/stream`

## Notes

- Task commands are seeded at startup into SQLite.
- Shayan commands are executed in the Shayan repo working directory.
- Shayan-specific code is isolated under `app/modules/shayan/`.
- Stop behavior: first toggle on running task is graceful stop.
- Stop behavior: second toggle is force stop.
