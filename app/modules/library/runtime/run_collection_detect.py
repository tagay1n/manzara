"""CLI entry point for library collection detection."""

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

from app.modules.library.collections import detect_collections  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect library collections")
    parser.add_argument(
        "--scan-limit",
        type=int,
        default=12000,
        help="Maximum number of metadata rows to scan",
    )
    parser.add_argument(
        "--min-items",
        type=int,
        default=2,
        help="Minimum items required per detected collection",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    payload = detect_collections(
        scan_limit=args.scan_limit,
        min_items=args.min_items,
    )
    print(json.dumps(payload, ensure_ascii=False))
    if not bool(payload.get("available", False)):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
