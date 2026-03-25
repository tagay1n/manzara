"""CLI entry point for library collection detection."""

from __future__ import annotations

import argparse
import json

from app.modules.library.collections import detect_collections


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

