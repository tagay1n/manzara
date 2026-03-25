"""Oscar stage runner entrypoint."""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Sequence

from app.db import Database
from app.settings import load_settings


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one Oscar stage.")
    parser.add_argument(
        "--stage",
        required=True,
        choices=["resolve_offsets_local", "download_ranges", "export_parquet"],
    )
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--artifacts-dir", required=True)
    parser.add_argument("--part-size-mb", type=int, default=1024)
    parser.add_argument("--snapshot", default=None)
    parser.add_argument("--limit", type=int, default=None)
    return parser.parse_args(argv)


def _python_bin(repo_path: Path) -> str:
    candidate = repo_path / ".venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return "python3"


def _resolve_stage(
    *,
    db: Database,
    repo: Path,
    artifacts: Path,
    snapshot_override: str | None,
    limit: int | None,
) -> int:
    oscar_app_dir = artifacts
    oscar_app_dir.mkdir(parents=True, exist_ok=True)
    _seed_snapshot_queue_from_sqlite(db=db, oscar_app_dir=oscar_app_dir)

    claimed = db.get_oscar_snapshot(snapshot_override) if snapshot_override else db.claim_next_oscar_snapshot()
    if snapshot_override and not claimed:
        db.upsert_oscar_snapshot(snapshot_override, source_label=snapshot_override, status="pending")
        claimed = db.get_oscar_snapshot(snapshot_override)
    if not claimed:
        print("oscar resolve_offsets_local: no pending snapshot")
        return 0

    snapshot_id = str(claimed.get("snapshot_id") or "").strip()
    if not snapshot_id:
        print("oscar resolve_offsets_local: empty snapshot id")
        return 1

    # Manual snapshot run is allowed even when queue row is not pending.
    if snapshot_override:
        db.set_oscar_snapshot_status(snapshot_id, "processing")

    db.upsert_oscar_snapshot_stage(snapshot_id, "resolve_offsets_local", "running")

    command = [
        _python_bin(repo),
        "-m",
        "app.cli",
        "resolve-offsets-local",
        "--snapshot",
        snapshot_id,
    ]
    if limit is not None and int(limit) > 0:
        command.extend(["--limit", str(int(limit))])

    env = os.environ.copy()
    env["OSCAR_APP_DIR"] = str(oscar_app_dir)

    print(f"oscar resolve_offsets_local: snapshot={snapshot_id}")
    print(f"oscar resolve_offsets_local: command={' '.join(command)}")
    print(f"oscar resolve_offsets_local: oscar_app_dir={oscar_app_dir}")

    result = subprocess.run(
        command,
        cwd=str(repo),
        env=env,
        text=True,
        check=False,
    )

    if int(result.returncode) == 0:
        db.upsert_oscar_snapshot_stage(snapshot_id, "resolve_offsets_local", "completed")
        db.set_oscar_snapshot_status(snapshot_id, "processing")
        print(f"oscar resolve_offsets_local: completed snapshot={snapshot_id}")
        return 0

    error_text = (result.stderr or result.stdout or "").strip()
    if not error_text:
        error_text = f"resolve-offsets-local failed with exit code {result.returncode}"
    db.upsert_oscar_snapshot_stage(
        snapshot_id,
        "resolve_offsets_local",
        "failed",
        error_text=error_text[:2000],
    )
    db.set_oscar_snapshot_status(snapshot_id, "failed", error_text=error_text[:2000])
    print(f"oscar resolve_offsets_local: failed snapshot={snapshot_id}")
    print(error_text[:2000])
    return 1


def _seed_snapshot_queue_from_sqlite(*, db: Database, oscar_app_dir: Path) -> None:
    """Best-effort seed of pending snapshots from Oscar's local SQLite state."""
    sqlite_path = oscar_app_dir / "state.sqlite"
    if not sqlite_path.exists():
        return
    try:
        conn = sqlite3.connect(str(sqlite_path))
        try:
            cur = conn.execute(
                """
                SELECT snapshot_name, hf_path
                FROM snapshots
                ORDER BY snapshot_name ASC
                """
            )
            added = 0
            for row in cur.fetchall():
                snapshot_name = str(row[0] or "").strip()
                hf_path = str(row[1] or "").strip() or None
                if not snapshot_name:
                    continue
                if db.get_oscar_snapshot(snapshot_name):
                    continue
                db.upsert_oscar_snapshot(
                    snapshot_name,
                    source_path=hf_path,
                    source_label=snapshot_name,
                    status="pending",
                )
                added += 1
        finally:
            conn.close()
    except Exception as exc:
        print(f"oscar resolve_offsets_local: queue seed skipped ({exc})")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo_path).expanduser()
    artifacts = Path(args.artifacts_dir).expanduser()
    artifacts.mkdir(parents=True, exist_ok=True)

    settings = load_settings()
    db = Database(settings.database_url, schema=settings.database_schema)

    print(f"repo_path={repo}")
    print(f"artifacts_dir={artifacts}")
    print(f"stage={args.stage}")

    if args.stage == "resolve_offsets_local":
        return _resolve_stage(
            db=db,
            repo=repo,
            artifacts=artifacts,
            snapshot_override=(str(args.snapshot).strip() if args.snapshot else None),
            limit=args.limit,
        )

    if args.stage == "export_parquet":
        print(f"part_size_mb={max(1, int(args.part_size_mb))}")
    print("status=not_implemented_yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
