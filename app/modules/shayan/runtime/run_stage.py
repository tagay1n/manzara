"""Shayan stage runner entrypoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from app.db import Database
from app.settings import load_settings

STAGES = ["scan_changes", "download_new"]
ARTIFACTS_PREFIX = "MANZARA_RUN_ARTIFACTS_JSON="
_EPISODES_SUMMARY_RE = re.compile(
    r"episodes:\s*"
    r"seen\s+(?P<seen>\d+)\s*\|\s*"
    r"listed\s+(?P<listed>\d+)\s*\|\s*"
    r"downloaded\s+(?P<downloaded>\d+)\s*\|\s*"
    r"skip-manifest\s+(?P<skipped_manifest>\d+)\s*\|\s*"
    r"skip-existing\s+(?P<skipped_existing>\d+)\s*\|\s*"
    r"failed\s+(?P<failed>\d+)\s*\|\s*"
    r"retries-used\s+(?P<retries_used>\d+)",
    re.IGNORECASE,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one Shayan stage.")
    parser.add_argument("--stage", required=True, choices=STAGES)
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--output-path", required=True)
    return parser.parse_args(argv)


def _python_bin(repo_path: Path) -> str:
    candidate = repo_path / ".venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return "python3"


def _run_command_streaming(command: Sequence[str], *, cwd: Path) -> Tuple[int, list[str]]:
    proc = subprocess.Popen(
        list(command),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []
    stream = proc.stdout
    if stream is not None:
        for raw in stream:
            line = raw.rstrip("\n")
            lines.append(line)
            print(line, flush=True)
        try:
            stream.close()
        except Exception:
            pass
    code = int(proc.wait())
    return code, lines


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _normalize_entries(payload: Dict[str, Any]) -> Dict[str, Any]:
    source = payload
    entries = source.get("entries")
    if isinstance(entries, dict):
        source = entries
    normalized: Dict[str, Any] = {}
    for key, value in source.items():
        entry_key = str(key or "").strip()
        if not entry_key:
            continue
        normalized[entry_key] = value if isinstance(value, dict) else {"value": value}
    return normalized


def _entry_hash(payload: Any) -> str:
    normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _hash_map(entries: Dict[str, Any]) -> Dict[str, str]:
    return {
        key: _entry_hash(value)
        for key, value in entries.items()
    }


def _run_id_from_env() -> Optional[int]:
    value = str(os.environ.get("MANZARA_TASK_RUN_ID") or "").strip()
    if not value:
        return None
    try:
        run_id = int(value)
    except Exception:
        return None
    return run_id if run_id > 0 else None


def _emit_artifacts(payload: Dict[str, Any]) -> None:
    print(ARTIFACTS_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def _scan_changes_stage(
    *,
    db: Database,
    repo_path: Path,
    output_path: Path,
) -> int:
    _ = output_path  # not used by scan stage
    previous = db.get_latest_shayan_snapshot_entry_hashes()
    with tempfile.TemporaryDirectory(prefix="manzara-shayan-scan-") as tmp_dir_raw:
        tmp_dir = Path(tmp_dir_raw)
        snapshot_file = tmp_dir / "latest.json"
        command = [
            _python_bin(repo_path),
            "app/main.py",
            "snapshot",
            "--category",
            "all",
            "--output-file",
            str(snapshot_file),
        ]
        print(f"shayan scan_changes: command={' '.join(command)}", flush=True)
        code, _lines = _run_command_streaming(command, cwd=repo_path)
        if code != 0:
            print(f"shayan scan_changes: failed exit_code={code}", flush=True)
            return 1

        payload = _read_json(snapshot_file)
        entries = _normalize_entries(payload)
        after = _hash_map(entries)

    before_ids = set(previous.keys())
    after_ids = set(after.keys())
    added = sorted(after_ids - before_ids)
    removed = sorted(before_ids - after_ids)
    changed = sorted(
        key for key in (before_ids & after_ids) if str(previous.get(key)) != str(after.get(key))
    )

    generated_at = str(payload.get("generated_at") or datetime.now(timezone.utc).isoformat())
    source = str(payload.get("source") or "https://tt.shayantv.ru")
    snapshot_id = db.create_shayan_snapshot(
        entries,
        run_id=_run_id_from_env(),
        source=source,
        generated_at=generated_at,
    )
    artifacts = {
        "kind": "shayan.snapshot_diff",
        "snapshot_id": snapshot_id,
        "episodes_before": len(previous),
        "episodes_after": len(after),
        "episodes_added": len(added),
        "episodes_changed": len(changed),
        "episodes_removed": len(removed),
        "added_sample_ids": added[:10],
        "changed_sample_ids": changed[:10],
        "removed_sample_ids": removed[:10],
    }
    _emit_artifacts(artifacts)
    print(
        "shayan scan_changes: completed "
        f"before={len(previous)} after={len(after)} "
        f"added={len(added)} changed={len(changed)} removed={len(removed)}",
        flush=True,
    )
    return 0


def _extract_download_metrics(lines: Sequence[str]) -> Dict[str, int]:
    for line in reversed([str(item or "") for item in lines]):
        match = _EPISODES_SUMMARY_RE.search(line)
        if not match:
            continue
        return {
            "seen": int(match.group("seen")),
            "listed": int(match.group("listed")),
            "downloaded": int(match.group("downloaded")),
            "skipped_manifest": int(match.group("skipped_manifest")),
            "skipped_existing": int(match.group("skipped_existing")),
            "failed": int(match.group("failed")),
            "retries_used": int(match.group("retries_used")),
        }
    return {}


def _download_new_stage(
    *,
    db: Database,
    repo_path: Path,
    output_path: Path,
) -> int:
    before_manifest = db.list_shayan_manifest_entries()
    before_hashes = _hash_map(before_manifest)

    with tempfile.TemporaryDirectory(prefix="manzara-shayan-download-") as tmp_dir_raw:
        tmp_dir = Path(tmp_dir_raw)
        status_file = tmp_dir / "status.json"
        status_file.write_text(
            json.dumps(before_manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        command = [
            _python_bin(repo_path),
            "app/main.py",
            "main",
            "--category",
            "all",
            "--output",
            str(output_path),
            "--status-file",
            str(status_file),
        ]
        print(f"shayan download_new: command={' '.join(command)}", flush=True)
        code, lines = _run_command_streaming(command, cwd=repo_path)
        if code != 0:
            print(f"shayan download_new: failed exit_code={code}", flush=True)
            return 1
        updated_manifest = _normalize_entries(_read_json(status_file))
        if not updated_manifest and before_manifest:
            print(
                "shayan download_new: failed to read updated status file "
                "(empty manifest after successful command)",
                flush=True,
            )
            return 1

    db.replace_shayan_manifest_entries(updated_manifest)
    after_hashes = _hash_map(updated_manifest)

    before_ids = set(before_hashes.keys())
    after_ids = set(after_hashes.keys())
    added = sorted(after_ids - before_ids)
    changed = sorted(
        key
        for key in (before_ids & after_ids)
        if str(before_hashes.get(key)) != str(after_hashes.get(key))
    )

    artifacts: Dict[str, Any] = {
        "kind": "shayan.download_summary",
        "manifest_before": len(before_hashes),
        "manifest_after": len(after_hashes),
        "manifest_added": len(added),
        "manifest_changed": len(changed),
    }
    metrics = _extract_download_metrics(lines)
    artifacts.update(metrics)
    _emit_artifacts(artifacts)
    print(
        "shayan download_new: completed "
        f"manifest_before={len(before_hashes)} manifest_after={len(after_hashes)} "
        f"manifest_added={len(added)} manifest_changed={len(changed)}",
        flush=True,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    db = Database(settings.database_url, schema=settings.database_schema)
    repo_path = Path(args.repo_path).expanduser()
    output_path = Path(args.output_path).expanduser()

    if args.stage == "scan_changes":
        return _scan_changes_stage(
            db=db,
            repo_path=repo_path,
            output_path=output_path,
        )

    if args.stage == "download_new":
        return _download_new_stage(
            db=db,
            repo_path=repo_path,
            output_path=output_path,
        )

    print(f"Unknown stage: {args.stage}", flush=True)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
