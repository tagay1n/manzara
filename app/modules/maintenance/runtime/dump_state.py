"""CLI entry point for the Maintenance database-state export task."""

from __future__ import annotations

import argparse
import re
import json
from uuid import uuid4
import signal
import tempfile
from pathlib import Path
from typing import Any

from app.task_runtime.logging import log_message
from app.artifacts import private_credentials_dir, workspace_dir
from app.modules.maintenance.catalog_sharing import SharingValidationError
from app.modules.maintenance.dump_state import StopRequested, run_dump
from app.postgres_engine import get_postgres_engine
from app.settings import load_settings

SCHEMA_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish the document catalog to Google Sheets."
    )
    parser.add_argument(
        "--validate-sharing",
        action="store_true",
        help="Block export unless every document matches the catalog sharing policy.",
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

    signal.signal(signal.SIGINT, request_stop)
    root = workspace_dir("maintenance", "catalog-export")
    credentials_dir = private_credentials_dir("google-drive")
    try:
        with tempfile.TemporaryDirectory(prefix="run-", dir=root) as temp_dir:
            summary = run_dump(
                engine=engine,
                workspace=Path(temp_dir),
                credentials_dir=credentials_dir,
                schema=settings.database_schema,
                validate_sharing=args.validate_sharing,
                should_stop=lambda: bool(stop_state["requested"]),
            )
        (root / f"run-{uuid4().hex}.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    except SharingValidationError as exc:
        log_message(str(exc))
        return 1
    except StopRequested as exc:
        log_message(f"dump state: stopped: {exc}")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
