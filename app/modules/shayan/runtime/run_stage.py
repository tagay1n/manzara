"""Shayan stage runner entrypoint."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
import posixpath
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple
import yaml

from app.db import Database
from app.run_artifact_channel import emit_run_artifact
from app.settings import load_settings

from yadisk_client import ConflictResolution, YaDisk


STAGES = ["scan_changes", "download_new", "upload_yadisk"]
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


def _contains_redacted(node: Any) -> bool:
    if isinstance(node, str):
        return "<REDACTED>" in node
    if isinstance(node, dict):
        return any(_contains_redacted(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_redacted(value) for value in node)
    return False


def _load_runtime_config_payload() -> Dict[str, Any]:
    env_override = str(os.environ.get("MANZARA_CONFIG_PATH") or "").strip()
    repo_root = Path(__file__).resolve().parents[4]
    candidates: list[Path]
    if env_override:
        candidates = [Path(env_override).expanduser()]
    else:
        candidates = [
            repo_root / "config.local.yaml",
            repo_root / "config.yaml",
        ]

    for path in candidates:
        if not path.exists():
            continue
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            continue
        if _contains_redacted(payload):
            continue
        return payload
    return {}


def _lookup_nested(payload: Dict[str, Any], *keys: str) -> Any:
    node: Any = payload
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _safe_value(value: Any) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class YaDiskUploadSettings:
    token: str
    category_target_dirs: Dict[str, str]


def _load_yadisk_upload_settings() -> YaDiskUploadSettings:
    payload = _load_runtime_config_payload()
    token = _safe_value(
        os.environ.get("SHAYAN_YADISK_OAUTH_TOKEN")
        or _lookup_nested(payload, "yandex", "disk", "oauth_token")
    )
    if not token:
        raise RuntimeError(
            "Yandex Disk OAuth token is missing. Set SHAYAN_YADISK_OAUTH_TOKEN "
            "or configure yandex.disk.oauth_token."
        )

    cartoons_target = _safe_value(
        os.environ.get("SHAYAN_YADISK_CARTOONS_TARGET_DIR")
        or _lookup_nested(payload, "yandex", "disk", "shayan", "cartoons")
    ).rstrip("/")
    shows_target = _safe_value(
        os.environ.get("SHAYAN_YADISK_SHOWS_TARGET_DIR")
        or _lookup_nested(payload, "yandex", "disk", "shayan", "shows")
    ).rstrip("/")
    if not cartoons_target or not shows_target:
        raise RuntimeError(
            "Yandex Disk Shayan target directories are missing. "
            "Configure yandex.disk.shayan.cartoons and yandex.disk.shayan.shows "
            "(or SHAYAN_YADISK_CARTOONS_TARGET_DIR / SHAYAN_YADISK_SHOWS_TARGET_DIR)."
        )
    return YaDiskUploadSettings(
        token=token,
        category_target_dirs={
            "cartoons": cartoons_target,
            "shows": shows_target,
        },
    )


def _create_yadisk_client(token: str) -> YaDisk:
    return YaDisk(token)


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


def _extract_local_file_path(payload: Dict[str, Any], output_path: Path) -> Optional[Path]:
    raw = str(payload.get("file") or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate
    return (output_path / candidate).resolve()


def _relative_video_path(local_file: Path, output_path: Path) -> str:
    try:
        rel = local_file.relative_to(output_path)
        return rel.as_posix().lstrip("/")
    except Exception:
        pass
    raw = local_file.as_posix()
    marker = "/videos/"
    idx = raw.rfind(marker)
    if idx >= 0:
        return raw[idx + 1 :].lstrip("/")
    return local_file.name


def _build_remote_upload_path(local_file: Path, output_path: Path, target_dir: str) -> str:
    rel = _relative_video_path(local_file, output_path)
    return posixpath.normpath(posixpath.join(target_dir, rel))


def _extract_category(payload: Dict[str, Any], local_file: Path, output_path: Path) -> str:
    direct = str(payload.get("category") or "").strip().lower()
    if direct:
        return direct
    rel = _relative_video_path(local_file, output_path)
    parts = [item for item in rel.strip("/").split("/") if item]
    if len(parts) >= 2 and parts[0] == "videos":
        return parts[1].strip().lower()
    if parts:
        return parts[0].strip().lower()
    return ""


def _relative_path_within_category(
    *,
    local_file: Path,
    output_path: Path,
    category: str,
) -> str:
    rel = _relative_video_path(local_file, output_path).strip("/")
    if not rel:
        return local_file.name
    prefixes = [
        f"videos/{category}/",
        f"{category}/",
    ]
    for prefix in prefixes:
        if rel.startswith(prefix):
            trimmed = rel[len(prefix):].strip("/")
            if trimmed:
                return trimmed
    return rel


def _resolve_target_dir_for_category(settings: YaDiskUploadSettings, category: str) -> str:
    category_key = str(category or "").strip().lower()
    if not category_key:
        return ""
    return str(settings.category_target_dirs.get(category_key) or "").strip()


def _calculate_local_md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - md5 required for Yandex Disk compatibility checks
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _upload_yadisk_stage(
    *,
    db: Database,
    output_path: Path,
) -> int:
    try:
        upload_settings = _load_yadisk_upload_settings()
    except Exception as exc:
        print(f"shayan upload_yadisk: failed settings: {exc}", flush=True)
        return 1

    client = _create_yadisk_client(upload_settings.token)
    try:
        valid = client.check_token()
    except Exception as exc:
        print(f"shayan upload_yadisk: token validation failed: {type(exc).__name__}: {exc}", flush=True)
        return 1
    if valid is False:
        print("shayan upload_yadisk: token validation failed: check_token returned false", flush=True)
        return 1

    candidates = db.list_shayan_manifest_upload_candidates(limit=5000)
    print(
        "shayan upload_yadisk: start "
        f"considered={len(candidates)} "
        f"target_dirs={json.dumps(upload_settings.category_target_dirs, ensure_ascii=False, sort_keys=True)}",
        flush=True,
    )
    uploaded = 0
    failed = 0
    missing_local = 0
    hash_mismatch = 0
    deleted_local = 0

    total = len(candidates)
    for idx, item in enumerate(candidates, start=1):
        entry_key = str(item.get("entry_key") or "").strip()
        payload_hash = str(item.get("payload_hash") or "").strip()
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if not entry_key or not payload_hash:
            continue

        local_file = _extract_local_file_path(payload, output_path)
        if local_file is None:
            db.mark_shayan_manifest_yadisk_failed(
                entry_key,
                error_text="local_file_missing_in_payload",
            )
            failed += 1
            missing_local += 1
            print(
                "shayan upload_yadisk: failed "
                f"progress={idx}/{total} entry_key={entry_key} reason=local_file_missing_in_payload",
                flush=True,
            )
            continue
        if not local_file.exists():
            db.mark_shayan_manifest_yadisk_failed(
                entry_key,
                error_text=f"local_file_missing:{local_file}",
            )
            failed += 1
            missing_local += 1
            print(
                "shayan upload_yadisk: failed "
                f"progress={idx}/{total} entry_key={entry_key} reason=local_file_missing "
                f"local_file={local_file}",
                flush=True,
            )
            continue

        category = _extract_category(payload, local_file, output_path)
        target_dir = _resolve_target_dir_for_category(upload_settings, category)
        if not target_dir:
            db.mark_shayan_manifest_yadisk_failed(
                entry_key,
                error_text=f"missing_target_dir_for_category:{category or 'unknown'}",
            )
            failed += 1
            print(
                "shayan upload_yadisk: failed "
                f"progress={idx}/{total} entry_key={entry_key} reason=missing_target_dir "
                f"category={category or 'unknown'}",
                flush=True,
            )
            continue
        rel_within_category = _relative_path_within_category(
            local_file=local_file,
            output_path=output_path,
            category=category,
        )
        remote_path = posixpath.normpath(posixpath.join(target_dir, rel_within_category))
        remote_dir = posixpath.dirname(remote_path)
        print(
            "shayan upload_yadisk: uploading "
            f"progress={idx}/{total} entry_key={entry_key} category={category or 'unknown'} "
            f"local_file={local_file} remote_path={remote_path}",
            flush=True,
        )
        try:
            uploaded_path, _remote_md5 = client.upload_or_replace(
                str(local_file),
                remote_dir=remote_dir,
                conflict_resolution=ConflictResolution.REPLACE_IF_DIFFERENT,
            )
            uploaded_remote_path = str(uploaded_path or remote_path)
            local_md5 = _calculate_local_md5(local_file)
            remote_md5 = str(_remote_md5 or "").strip().lower()
            if not remote_md5:
                remote_meta = client.get_meta_or_none(uploaded_remote_path, fields=["md5"])
                if isinstance(remote_meta, dict):
                    remote_md5 = str(remote_meta.get("md5") or "").strip().lower()
            if not remote_md5:
                db.mark_shayan_manifest_yadisk_failed(
                    entry_key,
                    error_text="remote_md5_missing_after_upload",
                )
                failed += 1
                print(
                    "shayan upload_yadisk: failed "
                    f"progress={idx}/{total} entry_key={entry_key} reason=remote_md5_missing",
                    flush=True,
                )
                continue
            if remote_md5 != local_md5:
                db.mark_shayan_manifest_yadisk_failed(
                    entry_key,
                    error_text=f"hash_mismatch local_md5={local_md5} remote_md5={remote_md5}",
                )
                failed += 1
                hash_mismatch += 1
                print(
                    "shayan upload_yadisk: failed "
                    f"progress={idx}/{total} entry_key={entry_key} reason=hash_mismatch "
                    f"local_md5={local_md5} remote_md5={remote_md5}",
                    flush=True,
                )
                continue
            try:
                local_file.unlink()
            except Exception as delete_exc:
                db.mark_shayan_manifest_yadisk_failed(
                    entry_key,
                    error_text=f"local_delete_failed:{type(delete_exc).__name__}: {delete_exc}",
                )
                failed += 1
                print(
                    "shayan upload_yadisk: failed "
                    f"progress={idx}/{total} entry_key={entry_key} reason=local_delete_failed "
                    f"error={type(delete_exc).__name__}: {delete_exc}",
                    flush=True,
                )
                continue
            db.mark_shayan_manifest_yadisk_uploaded(
                entry_key,
                remote_path=uploaded_remote_path,
                payload_hash=payload_hash,
            )
            uploaded += 1
            deleted_local += 1
            print(
                "shayan upload_yadisk: uploaded "
                f"progress={idx}/{total} entry_key={entry_key} category={category or 'unknown'} "
                f"remote_path={uploaded_remote_path} local_deleted=true",
                flush=True,
            )
        except Exception as exc:
            db.mark_shayan_manifest_yadisk_failed(
                entry_key,
                error_text=f"{type(exc).__name__}: {exc}",
            )
            failed += 1
            print(
                "shayan upload_yadisk: failed "
                f"progress={idx}/{total} entry_key={entry_key} "
                f"reason={type(exc).__name__}: {exc}",
                flush=True,
            )

    artifacts = {
        "kind": "shayan.upload_yadisk_summary",
        "target_dirs": dict(upload_settings.category_target_dirs),
        "considered": len(candidates),
        "uploaded": uploaded,
        "failed": failed,
        "missing_local": missing_local,
        "hash_mismatch": hash_mismatch,
        "deleted_local": deleted_local,
    }
    _emit_artifacts(artifacts)
    print(
        "shayan upload_yadisk: completed "
        f"considered={len(candidates)} uploaded={uploaded} failed={failed} "
        f"missing_local={missing_local} hash_mismatch={hash_mismatch} deleted_local={deleted_local}",
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

    if args.stage == "upload_yadisk":
        return _upload_yadisk_stage(
            db=db,
            output_path=output_path,
        )

    print(f"Unknown stage: {args.stage}", flush=True)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
