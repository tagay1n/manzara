"""Shayan stage runner entrypoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from app.db import Database
from app.run_artifact_channel import emit_run_artifact
from app.settings import load_settings

STAGES = ["scan_changes", "download_new"]
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
    if emit_run_artifact(payload):
        return
    print("shayan artifacts channel unavailable: MANZARA_RUN_ARTIFACT_PATH is not set", flush=True)


def _run_best_effort_snapshot(
    *,
    repo_path: Path,
    snapshot_file: Path,
    category: str,
) -> Tuple[int, list[str]]:
    runner_code = textwrap.dedent(
        """
        from __future__ import annotations

        import argparse
        from pathlib import Path

        import typer

        from app.main import (
            apply_program_filters,
            collect_programs,
            parse_episodes,
            save_status,
            snapshot_to_dict,
        )
        from app.models import SnapshotEpisode


        def parse_args() -> argparse.Namespace:
            parser = argparse.ArgumentParser(description="Best-effort snapshot collector")
            parser.add_argument("--category", default="all")
            parser.add_argument("--output-file", required=True)
            parser.add_argument("--program-filter", default="")
            parser.add_argument("--limit-programs", type=int, default=0)
            parser.add_argument("--limit-episodes", type=int, default=0)
            return parser.parse_args()


        def main() -> int:
            args = parse_args()
            programs = apply_program_filters(
                collect_programs(args.category),
                program_filter=args.program_filter,
                limit_programs=args.limit_programs,
            )
            entries = []
            skipped = []
            for program in programs:
                typer.secho(f"[snapshot] {program.title} ({program.url})", fg="cyan")
                try:
                    episodes = parse_episodes(program)
                except Exception as exc:  # noqa: BLE001
                    message = f"{type(exc).__name__}: {exc}"
                    skipped.append(
                        {
                            "category": str(program.category),
                            "program": str(program.title),
                            "url": str(program.url),
                            "error": message,
                        }
                    )
                    typer.secho(
                        f"[warning] skip program={program.url} reason={message}",
                        fg="yellow",
                    )
                    continue
                if args.limit_episodes:
                    episodes = episodes[: int(args.limit_episodes)]
                for ep in episodes:
                    entries.append(
                        SnapshotEpisode(
                            category=ep.program.category,
                            program=ep.program.title,
                            season=ep.season,
                            episode=ep.episode,
                            title=ep.title,
                            hls=ep.hls,
                            program_url=ep.program.url,
                        )
                    )

            output_path = Path(args.output_file).expanduser()
            save_status(output_path, snapshot_to_dict(entries))
            typer.secho(
                f"Saved snapshot with {len(entries)} episodes to {output_path}",
                fg="green",
            )
            print(
                "manzara_best_effort_snapshot:"
                f" programs_total={len(programs)}"
                f" programs_skipped={len(skipped)}"
                f" episodes_total={len(entries)}",
                flush=True,
            )
            return 0


        if __name__ == "__main__":
            raise SystemExit(main())
        """
    ).strip()
    command = [
        _python_bin(repo_path),
        "-c",
        runner_code,
        "--category",
        str(category),
        "--output-file",
        str(snapshot_file),
    ]
    return _run_command_streaming(command, cwd=repo_path)


def _scan_changes_stage(
    *,
    db: Database,
    repo_path: Path,
    output_path: Path,
) -> int:
    _ = output_path  # not used by scan stage
    previous_entries = db.get_latest_shayan_snapshot_entries()
    previous = _hash_map(previous_entries)
    with tempfile.TemporaryDirectory(prefix="manzara-shayan-scan-") as tmp_dir_raw:
        tmp_dir = Path(tmp_dir_raw)
        snapshot_file = tmp_dir / "latest.json"
        print(
            "shayan scan_changes: command=best-effort snapshot collector "
            f"category=all output_file={snapshot_file}",
            flush=True,
        )
        code, _lines = _run_best_effort_snapshot(
            repo_path=repo_path,
            snapshot_file=snapshot_file,
            category="all",
        )
        if code != 0:
            print(f"shayan scan_changes: failed exit_code={code}", flush=True)
            return 1

        payload = _read_json(snapshot_file)
        entries = _normalize_entries(payload)
        if not entries:
            print(
                "shayan scan_changes: failed empty snapshot (no episodes parsed); "
                "previous snapshot preserved",
                flush=True,
            )
            return 1
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
    run_id = _run_id_from_env()
    snapshot_id = db.create_shayan_snapshot(
        entries,
        run_id=run_id,
        source=source,
        generated_at=generated_at,
    )
    if run_id is not None:
        db.replace_shayan_run_changes(
            run_id,
            _build_change_rows(
                before_entries=previous_entries,
                after_entries=entries,
                added_ids=added,
                changed_ids=changed,
                removed_ids=removed,
            ),
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

def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


def _extract_meta(entry_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "entry_key": entry_key,
        "category": str(payload.get("category") or "").strip() or None,
        "program": str(payload.get("program") or "").strip() or None,
        "season": _to_int(payload.get("season")),
        "episode": _to_int(payload.get("episode")),
        "title": str(payload.get("title") or "").strip() or None,
    }


def _build_change_rows(
    *,
    before_entries: Dict[str, Any],
    after_entries: Dict[str, Any],
    added_ids: Sequence[str],
    changed_ids: Sequence[str],
    removed_ids: Sequence[str],
) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    for entry_id in added_ids:
        new_payload = after_entries.get(entry_id) or {}
        meta = _extract_meta(entry_id, new_payload)
        rows.append(
            {
                "change_type": "added",
                **meta,
                "old_payload": {},
                "new_payload": new_payload,
            }
        )
    for entry_id in changed_ids:
        old_payload = before_entries.get(entry_id) or {}
        new_payload = after_entries.get(entry_id) or {}
        meta = _extract_meta(entry_id, new_payload if isinstance(new_payload, dict) else old_payload)
        rows.append(
            {
                "change_type": "changed",
                **meta,
                "old_payload": old_payload,
                "new_payload": new_payload,
            }
        )
    for entry_id in removed_ids:
        old_payload = before_entries.get(entry_id) or {}
        meta = _extract_meta(entry_id, old_payload)
        rows.append(
            {
                "change_type": "removed",
                **meta,
                "old_payload": old_payload,
                "new_payload": {},
            }
        )
    rows.sort(
        key=lambda item: (
            str(item.get("program") or ""),
            int(item.get("season") or 0),
            int(item.get("episode") or 0),
            str(item.get("title") or ""),
            str(item.get("entry_key") or ""),
            str(item.get("change_type") or ""),
        )
    )
    return rows


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
