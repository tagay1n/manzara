# PostgreSQL pooling and endpoint benchmark

Recorded: 2026-09-03  
Baseline revision: `8c492bb`

No database URLs, hosts, users, passwords, or key values are recorded here.

## Counting method

Baseline counts come from the repository call graph at `8c492bb`. Each
`CoreRepository._connect()` was a new physical connection and executed
`CREATE SCHEMA IF NOT EXISTS` plus `SET search_path` before the repository SQL.
The Gemini count uses the configured key count (50) without recording key data.

After-change counts come from the credential-free cumulative pool metrics in
`CoreRepository.get_pool_metrics()`, measured by
`tests/test_postgres_endpoint_efficiency.py` against a generated
`manzara_test_*` schema. "Checkout" means a borrow from the pool; "new physical"
means a TCP/TLS PostgreSQL connection created during the warm request.

## Runtime ownership

`app.postgres_engine` owns the sole SQLAlchemy engine for each database/schema
pair inside a process. The shared `Database` facade and flow-specific
repositories use that same bounded pool; application code must not call
`sqlalchemy.create_engine()` directly. Pools use pre-ping, recycle stale cloud
connections, and disable overflow.

Task subprocesses cannot share live DBAPI connections with the web process.
The task runner therefore sets `MANZARA_DB_POOL_SIZE=1` for every child process.
Worker threads serialize their short PostgreSQL sections through that one
connection, while network/model work remains concurrent. The web process keeps
the configured `database_pool_size` (four by default).

Tests use one local PostgreSQL Testcontainer per pytest session. Production or
cloud database URLs are not a test fallback.

| Endpoint | Baseline physical connections | Baseline repository/business SQL | Baseline request-time setup SQL | Warm checkouts after | Warm SQL after | New physical after |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `/api/health` | 0 | 0 | 0 | 0 | 0 | 0 |
| `/api/tasks` | 8-9 | 8-9 | 16-18 | 5 | 5 | 0 |
| `/api/gemini/state` | 6 | 107 | 12 | 5 | 6 | 0 |
| `/api/dashboard` | 19 | 19 | 37 | 7 | 7 | 0 |

The Tasks range depends on whether a previous conveyor run exists. The baseline
dashboard count includes the Collections overview, which created one separate
SQLAlchemy connection and ran its own `SET search_path`; that read now uses the
shared pool.

## Timing evidence

The supplied pre-change Aiven measurements were:

| Endpoint | Before |
| --- | ---: |
| `/api/health` | 0.01-0.10 s |
| `/api/tasks` | 12.8-13.4 s |
| `/api/gemini/state` | 19.8-20.0 s |
| `/api/dashboard` | 26.2-27.1 s |

Warm isolated-schema measurements after pooling and consolidation:

| Endpoint | After |
| --- | ---: |
| `/api/health` | 0.005 s |
| `/api/tasks` | 1.588 s |
| `/api/gemini/state` | 1.945 s |
| `/api/dashboard` | 2.240 s |

Run the same credential-safe benchmark with:

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q -s tests/test_postgres_endpoint_efficiency.py
```

The fixture creates, migrates, truncates, and drops only its generated test
schema. It never uses the configured production schema.
