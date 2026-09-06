"""CLI entry point for the Maintenance database-state export task."""

from __future__ import annotations

import argparse
import re
import signal
import tempfile
from pathlib import Path
from typing import Any

from app.artifacts import private_credentials_dir, workspace_dir
from app.modules.maintenance.dump_state import StopRequested, run_dump
from app.postgres_engine import get_postgres_engine
from app.run_artifact_channel import emit_run_artifact
from app.settings import load_settings

SCHEMA_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export the document catalog to Google Drive and Sheets."
    )
    parser.add_argument(
        "--legacy-credentials-dir",
        type=Path,
        default=None,
        help="Read-only fallback directory for existing monocorpus OAuth files.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    settings = load_settings()
    if not SCHEMA_PATTERN.fullmatch(settings.database_schema):
        raise ValueError(f"Invalid database schema: {settings.database_schema!r}")
    engine = get_postgres_engine(
        settings.database_url,
        schema=settings.database_schema,
        pool_size=settings.database_pool_size,
    )
    stop_state = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_state["requested"] = True
        print("dump state: graceful stop requested; finishing current operation", flush=True)

    signal.signal(signal.SIGINT, request_stop)
    root = workspace_dir("maintenance", "catalog-export")
    credentials_dir = private_credentials_dir("google-drive")
    try:
        with tempfile.TemporaryDirectory(prefix="run-", dir=root) as temp_dir:
            summary = run_dump(
                engine=engine,
                workspace=Path(temp_dir),
                credentials_dir=credentials_dir,
                legacy_credentials_dir=args.legacy_credentials_dir,
                should_stop=lambda: bool(stop_state["requested"]),
            )
        emit_run_artifact(summary)
        return 0
    except StopRequested as exc:
        print(f"dump state: stopped: {exc}", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
