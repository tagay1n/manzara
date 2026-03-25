"""Oscar stage runner skeleton.

This is intentionally lightweight for migration step 2.
Runtime-heavy Oscar logic will be ported in later steps.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one Oscar stage (skeleton).")
    parser.add_argument(
        "--stage",
        required=True,
        choices=["resolve_offsets_local", "download_ranges", "export_parquet"],
    )
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--artifacts-dir", required=True)
    parser.add_argument("--part-size-mb", type=int, default=1024)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = Path(args.repo_path).expanduser()
    artifacts = Path(args.artifacts_dir).expanduser()
    artifacts.mkdir(parents=True, exist_ok=True)

    print(f"oscar stage skeleton: {args.stage}")
    print(f"repo_path={repo}")
    print(f"artifacts_dir={artifacts}")
    if args.stage == "export_parquet":
        print(f"part_size_mb={max(1, int(args.part_size_mb))}")
    print("status=not_implemented_yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

