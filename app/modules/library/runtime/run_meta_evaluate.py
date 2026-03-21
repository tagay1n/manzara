"""Entry point for embedded monocorpus metadata evaluation runtime."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MetaEvalArgs:
    """Arguments expected by metadata evaluation runtime."""

    batch_size: int
    workers: int
    dry_run: bool
    excerpt_chars: int


def _parse_args() -> MetaEvalArgs:
    parser = argparse.ArgumentParser(description="Run monocorpus metadata evaluate")
    parser.add_argument("--batch-size", type=int, default=300)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--excerpt-chars", type=int, default=500)
    parsed = parser.parse_args()
    return MetaEvalArgs(
        batch_size=parsed.batch_size,
        workers=parsed.workers,
        dry_run=parsed.dry_run,
        excerpt_chars=parsed.excerpt_chars,
    )


def main() -> None:
    runtime_root = Path(__file__).resolve().parent
    sys.path.insert(0, str(runtime_root))
    from metadata.evaluation import evaluate

    evaluate(_parse_args())


if __name__ == "__main__":
    main()
