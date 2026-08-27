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


def _bootstrap_import_paths() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    runtime_root = Path(__file__).resolve().parent
    for path in (repo_root, runtime_root):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)


def _parse_args() -> MetaEvalArgs:
    from app.gemini_workers import resolve_gemini_workers

    parser = argparse.ArgumentParser(description="Run monocorpus metadata evaluate")
    parser.add_argument("--batch-size", type=int, default=300)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--excerpt-chars", type=int, default=500)
    parsed = parser.parse_args()
    return MetaEvalArgs(
        batch_size=parsed.batch_size,
        workers=resolve_gemini_workers(parsed.workers),
        dry_run=parsed.dry_run,
        excerpt_chars=parsed.excerpt_chars,
    )


def main() -> None:
    _bootstrap_import_paths()
    from metadata.evaluation import evaluate

    evaluate(_parse_args())


if __name__ == "__main__":
    main()
