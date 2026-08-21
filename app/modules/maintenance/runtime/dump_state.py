"""CLI entry point for the Maintenance database-state export task."""

from __future__ import annotations

import argparse
import re
import signal
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine

from app.artifacts import flow_artifacts_dir
from app.modules.maintenance.dump_state import StopRequested, run_dump
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
    engine = create_engine(
        settings.database_url,
        connect_args={
            "options": f"-csearch_path={settings.database_schema},public"
        },
    )
    stop_state = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_state["requested"] = True
        print("dump state: graceful stop requested; finishing current operation", flush=True)

    signal.signal(signal.SIGINT, request_stop)
    root = flow_artifacts_dir("maintenance") / "dump-state"
    root.mkdir(parents=True, exist_ok=True)
    credentials_dir = flow_artifacts_dir("maintenance") / "credentials"
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
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
