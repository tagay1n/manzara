"""Oscar stage runner entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Sequence

import yaml
from app.db import Database
from app.settings import load_settings

STAGES = [
    "discover_snapshots",
    "resolve_offsets_local",
    "download_ranges",
    "export_parquet",
    "upload_dataset",
]
REDACTED_SENTINEL = "<REDACTED>"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one Oscar stage.")
    parser.add_argument(
        "--stage",
        required=True,
        choices=STAGES,
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
    return _run_cli_stage(
        db=db,
        repo=repo,
        artifacts=artifacts,
        stage_name="resolve_offsets_local",
        cli_command="resolve-offsets-local",
        no_candidate_message="oscar resolve_offsets_local: no pending snapshot",
        snapshot_override=snapshot_override,
        limit=limit,
    )


def _discover_snapshots_stage(
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

    if snapshot_override:
        print("oscar discover_snapshots: --snapshot is ignored for discovery stage")
    if limit is not None and int(limit) > 0:
        print("oscar discover_snapshots: --limit is ignored for discovery stage")

    command = [
        _python_bin(repo),
        "-m",
        "app.cli",
        "ingest",
    ]
    env = os.environ.copy()
    env["OSCAR_APP_DIR"] = str(oscar_app_dir)

    print(f"oscar discover_snapshots: command={' '.join(command)}")
    print(f"oscar discover_snapshots: oscar_app_dir={oscar_app_dir}")
    result = subprocess.run(
        command,
        cwd=str(repo),
        env=env,
        text=True,
        check=False,
    )
    if int(result.returncode) == 0:
        _seed_snapshot_queue_from_sqlite(db=db, oscar_app_dir=oscar_app_dir)
        print("oscar discover_snapshots: completed")
        return 0

    error_text = (result.stderr or result.stdout or "").strip()
    if not error_text:
        error_text = f"ingest failed with exit code {result.returncode}"
    print("oscar discover_snapshots: failed")
    print(error_text[:2000])
    return 1


def _download_ranges_stage(
    *,
    db: Database,
    repo: Path,
    artifacts: Path,
    snapshot_override: str | None,
    limit: int | None,
) -> int:
    return _run_cli_stage(
        db=db,
        repo=repo,
        artifacts=artifacts,
        stage_name="download_ranges",
        cli_command="download-ranges",
        no_candidate_message="oscar download_ranges: no snapshot ready after resolve_offsets_local",
        snapshot_override=snapshot_override,
        limit=limit,
        required_stage="resolve_offsets_local",
    )


def _upload_dataset_stage(
    *,
    db: Database,
    repo: Path,
    artifacts: Path,
    snapshot_override: str | None,
) -> int:
    oscar_app_dir = artifacts
    oscar_app_dir.mkdir(parents=True, exist_ok=True)
    _seed_snapshot_queue_from_sqlite(db=db, oscar_app_dir=oscar_app_dir)

    if snapshot_override:
        claimed = db.get_oscar_snapshot(snapshot_override)
        if not claimed:
            print(f"oscar upload_dataset: snapshot not found: {snapshot_override}")
            return 1
        db.set_oscar_snapshot_status(snapshot_override, "processing")
    else:
        claimed = db.claim_next_oscar_snapshot_for_stage(
            "upload_dataset",
            required_stage="export_parquet",
            allowed_snapshot_statuses=["completed"],
        )

    if not claimed:
        print("oscar upload_dataset: no snapshot ready after export_parquet")
        return 0

    snapshot_id = str(claimed.get("snapshot_id") or "").strip()
    if not snapshot_id:
        print("oscar upload_dataset: empty snapshot id")
        return 1

    db.upsert_oscar_snapshot_stage(snapshot_id, "upload_dataset", "running")
    try:
        upload_result = _upload_snapshot_dataset(
            snapshot_id=snapshot_id,
            oscar_app_dir=oscar_app_dir,
            source_repo=repo,
        )
    except Exception as exc:
        error_text = str(exc).strip() or "upload_dataset failed"
        db.upsert_oscar_snapshot_stage(
            snapshot_id,
            "upload_dataset",
            "failed",
            error_text=error_text[:2000],
        )
        db.set_oscar_snapshot_status(snapshot_id, "failed", error_text=error_text[:2000])
        print(f"oscar upload_dataset: failed snapshot={snapshot_id}")
        print(error_text[:2000])
        return 1

    db.upsert_oscar_snapshot_stage(snapshot_id, "upload_dataset", "completed")
    db.set_oscar_snapshot_status(snapshot_id, "completed")
    print(
        "oscar upload_dataset: completed "
        f"snapshot={snapshot_id} files={int(upload_result.get('uploaded_files') or 0)}"
    )
    return 0


def _export_parquet_stage(
    *,
    db: Database,
    repo: Path,
    artifacts: Path,
    snapshot_override: str | None,
    limit: int | None,
    part_size_mb: int,
) -> int:
    return _run_cli_stage(
        db=db,
        repo=repo,
        artifacts=artifacts,
        stage_name="export_parquet",
        cli_command="export-parquet",
        no_candidate_message="oscar export_parquet: no snapshot ready after download_ranges",
        snapshot_override=snapshot_override,
        limit=limit,
        required_stage="download_ranges",
        extra_args=["--split", str(max(1, int(part_size_mb)))],
        success_snapshot_status="completed",
    )


def _run_cli_stage(
    *,
    db: Database,
    repo: Path,
    artifacts: Path,
    stage_name: str,
    cli_command: str,
    no_candidate_message: str,
    snapshot_override: str | None,
    limit: int | None,
    required_stage: str | None = None,
    extra_args: Sequence[str] | None = None,
    success_snapshot_status: str = "processing",
) -> int:
    oscar_app_dir = artifacts
    oscar_app_dir.mkdir(parents=True, exist_ok=True)
    _seed_snapshot_queue_from_sqlite(db=db, oscar_app_dir=oscar_app_dir)

    if snapshot_override:
        claimed = db.get_oscar_snapshot(snapshot_override)
        if not claimed:
            db.upsert_oscar_snapshot(snapshot_override, source_label=snapshot_override, status="pending")
            claimed = db.get_oscar_snapshot(snapshot_override)
    elif required_stage:
        claimed = db.claim_next_oscar_snapshot_for_stage(
            stage_name,
            required_stage=required_stage,
        )
    else:
        claimed = db.claim_next_oscar_snapshot()

    if not claimed:
        print(no_candidate_message)
        return 0

    snapshot_id = str(claimed.get("snapshot_id") or "").strip()
    if not snapshot_id:
        print(f"oscar {stage_name}: empty snapshot id")
        return 1

    if snapshot_override:
        db.set_oscar_snapshot_status(snapshot_id, "processing")

    db.upsert_oscar_snapshot_stage(snapshot_id, stage_name, "running")

    command = [
        _python_bin(repo),
        "-m",
        "app.cli",
        cli_command,
        "--snapshot",
        snapshot_id,
    ]
    if limit is not None and int(limit) > 0:
        command.extend(["--limit", str(int(limit))])
    if extra_args:
        command.extend([str(item) for item in extra_args if str(item).strip()])

    env = os.environ.copy()
    env["OSCAR_APP_DIR"] = str(oscar_app_dir)

    print(f"oscar {stage_name}: snapshot={snapshot_id}")
    print(f"oscar {stage_name}: command={' '.join(command)}")
    print(f"oscar {stage_name}: oscar_app_dir={oscar_app_dir}")

    result = subprocess.run(
        command,
        cwd=str(repo),
        env=env,
        text=True,
        check=False,
    )

    if int(result.returncode) == 0:
        db.upsert_oscar_snapshot_stage(snapshot_id, stage_name, "completed")
        db.set_oscar_snapshot_status(snapshot_id, success_snapshot_status)
        print(f"oscar {stage_name}: completed snapshot={snapshot_id}")
        return 0

    error_text = (result.stderr or result.stdout or "").strip()
    if not error_text:
        error_text = f"{cli_command} failed with exit code {result.returncode}"
    db.upsert_oscar_snapshot_stage(
        snapshot_id,
        stage_name,
        "failed",
        error_text=error_text[:2000],
    )
    db.set_oscar_snapshot_status(snapshot_id, "failed", error_text=error_text[:2000])
    print(f"oscar {stage_name}: failed snapshot={snapshot_id}")
    print(error_text[:2000])
    return 1


def _safe_config_value(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if REDACTED_SENTINEL in text:
        return None
    return text


def _lookup_nested(data: Dict[str, Any], *keys: str) -> Any:
    node: Any = data
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _iter_upload_config_payloads(source_repo: Path):
    env_override = _safe_config_value(os.environ.get("MANZARA_CONFIG_PATH"))
    manzara_root = Path(__file__).resolve().parents[4]

    candidates: list[Path] = []
    if env_override:
        candidates.append(Path(env_override).expanduser())
    candidates.extend(
        [
            manzara_root / "config.local.yaml",
            manzara_root / "config.yaml",
            source_repo / "config.local.yaml",
            source_repo / "config.yaml",
        ]
    )

    seen: set[Path] = set()
    for candidate in candidates:
        expanded = candidate.expanduser().resolve()
        if expanded in seen or not expanded.exists():
            continue
        seen.add(expanded)
        try:
            payload = yaml.safe_load(expanded.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if isinstance(payload, dict):
            yield payload


def _resolve_upload_hf_settings(source_repo: Path) -> tuple[str, str]:
    repo_id = _safe_config_value(os.environ.get("OSCAR_HF_UPLOAD_REPO"))
    token = _safe_config_value(os.environ.get("OSCAR_HF_UPLOAD_TOKEN")) or _safe_config_value(
        os.environ.get("HF_TOKEN")
    )

    for payload in _iter_upload_config_payloads(source_repo):
        if not repo_id:
            repo_id = _safe_config_value(
                _lookup_nested(payload, "oscar", "hf_upload", "repo")
                or _lookup_nested(payload, "hf_upload", "repo")
            )
        if not token:
            token = _safe_config_value(
                _lookup_nested(payload, "oscar", "hf_upload", "token")
                or _lookup_nested(payload, "hf_upload", "token")
                or _lookup_nested(payload, "hf", "token")
            )
        if repo_id and token:
            break

    if not repo_id:
        raise RuntimeError(
            "Missing Oscar HF upload repo. Set OSCAR_HF_UPLOAD_REPO or config oscar.hf_upload.repo."
        )
    if not token:
        raise RuntimeError(
            "Missing Oscar HF upload token. Set OSCAR_HF_UPLOAD_TOKEN/HF_TOKEN or config oscar.hf_upload.token."
        )
    return repo_id, token


def _snapshot_parquet_files(oscar_app_dir: Path, snapshot_id: str) -> list[Path]:
    parquet_dir = oscar_app_dir / "parquet"
    if not parquet_dir.exists():
        return []

    candidates: list[Path] = []
    bases = [snapshot_id]
    if snapshot_id.startswith("CC-MAIN-"):
        trimmed = snapshot_id[len("CC-MAIN-") :].strip()
        if trimmed:
            bases.append(trimmed)
    for base in bases:
        direct = parquet_dir / f"{base}.parquet"
        if direct.exists():
            candidates.append(direct)
        candidates.extend(sorted(parquet_dir.glob(f"{base}_part*.parquet")))

    unique: dict[str, Path] = {}
    for path in candidates:
        unique[str(path.resolve())] = path
    return sorted(unique.values(), key=lambda item: item.name)


def _upload_snapshot_dataset(
    *,
    snapshot_id: str,
    oscar_app_dir: Path,
    source_repo: Path,
) -> Dict[str, Any]:
    from huggingface_hub import HfApi

    repo_id, token = _resolve_upload_hf_settings(source_repo)
    files = _snapshot_parquet_files(oscar_app_dir, snapshot_id)
    if not files:
        raise RuntimeError(
            f"No parquet files found for snapshot {snapshot_id} under {oscar_app_dir / 'parquet'}"
        )

    api = HfApi(token=token)
    remote_prefix = f"oscar/{snapshot_id}"
    for file_path in files:
        api.upload_file(
            path_or_fileobj=str(file_path),
            path_in_repo=f"{remote_prefix}/{file_path.name}",
            repo_id=repo_id,
            repo_type="dataset",
            token=token,
            commit_message=f"oscar upload {snapshot_id}",
        )

    manifest = {
        "snapshot_id": snapshot_id,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "repo_id": repo_id,
        "remote_prefix": remote_prefix,
        "files": [path.name for path in files],
    }
    manifest_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            delete=False,
        ) as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            manifest_path = Path(handle.name)
        api.upload_file(
            path_or_fileobj=str(manifest_path),
            path_in_repo=f"{remote_prefix}/manifest.json",
            repo_id=repo_id,
            repo_type="dataset",
            token=token,
            commit_message=f"oscar upload manifest {snapshot_id}",
        )
    finally:
        if manifest_path and manifest_path.exists():
            manifest_path.unlink(missing_ok=True)

    return {
        "repo_id": repo_id,
        "uploaded_files": len(files),
        "remote_prefix": remote_prefix,
    }


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
        print(f"oscar queue seed skipped ({exc})")


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
    if args.stage == "discover_snapshots":
        return _discover_snapshots_stage(
            db=db,
            repo=repo,
            artifacts=artifacts,
            snapshot_override=(str(args.snapshot).strip() if args.snapshot else None),
            limit=args.limit,
        )
    if args.stage == "download_ranges":
        return _download_ranges_stage(
            db=db,
            repo=repo,
            artifacts=artifacts,
            snapshot_override=(str(args.snapshot).strip() if args.snapshot else None),
            limit=args.limit,
        )

    if args.stage == "export_parquet":
        return _export_parquet_stage(
            db=db,
            repo=repo,
            artifacts=artifacts,
            snapshot_override=(str(args.snapshot).strip() if args.snapshot else None),
            limit=args.limit,
            part_size_mb=max(1, int(args.part_size_mb)),
        )
    if args.stage == "upload_dataset":
        return _upload_dataset_stage(
            db=db,
            repo=repo,
            artifacts=artifacts,
            snapshot_override=(str(args.snapshot).strip() if args.snapshot else None),
        )
    print("status=not_implemented_yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
