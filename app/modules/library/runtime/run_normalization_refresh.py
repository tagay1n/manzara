"""CLI entry point for library normalization suggestion refresh."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path


def _bootstrap_repo_root() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)


_bootstrap_repo_root()

from app.db import Database  # noqa: E402
from app.gemini_workers import resolve_gemini_workers  # noqa: E402
from app.modules.library.normalization import refresh_suggestions  # noqa: E402
from app.settings import load_settings  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh normalization suggestions")
    parser.add_argument(
        "--entity-type",
        choices=["personality", "publisher"],
        required=True,
        help="Entity domain to refresh",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=180,
        help="Max unresolved aliases to process",
    )
    parser.add_argument(
        "--no-gemini",
        action="store_true",
        help="Disable Gemini-assisted suggestions",
    )
    parser.add_argument("--workers", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    workers = resolve_gemini_workers(args.workers)
    stop = {"requested": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("requested", True))
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("requested", True))
    settings = load_settings()
    db = Database(settings.database_url, schema=settings.database_schema)
    db.init_schema()
    result = refresh_suggestions(
        db,
        args.entity_type,
        limit=args.limit,
        use_gemini=not bool(args.no_gemini),
        workers=workers,
        should_stop=lambda: bool(stop["requested"]),
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
