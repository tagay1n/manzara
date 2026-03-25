"""CLI entry point for applying approved collection overrides."""

from __future__ import annotations

import argparse
import json

from app.modules.library.collections import apply_collection_overrides


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

