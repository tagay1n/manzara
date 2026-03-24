"""CLI entry point for library normalization suggestion refresh."""

from __future__ import annotations

import argparse
import json

from app.db import Database
from app.modules.library.normalization import refresh_suggestions
from app.settings import load_settings


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
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    settings = load_settings()
    db = Database(settings.db_path)
    db.init_schema()
    result = refresh_suggestions(
        db,
        args.entity_type,
        limit=args.limit,
        use_gemini=not bool(args.no_gemini),
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

