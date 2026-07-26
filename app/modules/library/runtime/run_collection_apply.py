"""CLI entry point for applying approved collection overrides."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _bootstrap_repo_root() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)


_bootstrap_repo_root()

from app.modules.library.collections import apply_collection_overrides  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply approved collection overrides")
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Maximum number of approved collections to process",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    payload = apply_collection_overrides(collection_limit=args.limit)
    print(json.dumps(payload, ensure_ascii=False))
    if not bool(payload.get("available", False)):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
