"""S3 verification helpers for pgBackRest backup runs."""

from __future__ import annotations

import re
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import yaml
from boto3 import Session

REDACTED_SENTINEL = "<REDACTED>"
DEFAULT_ENDPOINT = "https://storage.yandexcloud.net"
DEFAULT_MONOCORPUS_REPO = Path("/home/tans1q/projects/monocorpus")

_LABEL_RE = re.compile(r"new backup label\s*=\s*([A-Za-z0-9._-]+)", re.IGNORECASE)
_BUCKET_RE = re.compile(r"--repo1-s3-bucket=([^\s]+)")
_ENDPOINT_RE = re.compile(r"--repo1-s3-endpoint=([^\s]+)")
_STANZA_RE = re.compile(r"--stanza=([^\s]+)")


def verify_backup_objects_in_s3(
    log_lines: Iterable[str],
    *,
    label: Optional[str] = None,
    bucket: Optional[str] = None,
    stanza: Optional[str] = None,
    endpoint: Optional[str] = None,
    config_path: Optional[Path] = None,
    monocorpus_repo_path: Optional[Path] = None,
    max_scan_keys: int = 5000,
) -> Dict[str, Any]:
    """Verify that objects for one backup label exist in S3 bucket."""
    markers = extract_backup_markers(log_lines)
    resolved_label = str(label or markers.get("label") or "").strip()
    resolved_bucket = str(bucket or markers.get("bucket") or "").strip()
    resolved_stanza = str(stanza or markers.get("stanza") or "").strip()
    resolved_endpoint = str(endpoint or markers.get("endpoint") or DEFAULT_ENDPOINT).strip()

    if not resolved_label:
        return {"ok": False, "error": "backup label not found in logs or arguments"}
    if not resolved_bucket:
        return {"ok": False, "error": "S3 bucket not found in logs or arguments"}
    if not resolved_stanza:
        return {"ok": False, "error": "pgBackRest stanza not found in logs or arguments"}

    credentials = load_s3_credentials(
        config_path=config_path,
        monocorpus_repo_path=monocorpus_repo_path,
    )
    if not credentials.get("ok"):
        return credentials

    session = Session()
    s3 = session.client(
        service_name="s3",
        aws_access_key_id=credentials["aws_access_key_id"],
        aws_secret_access_key=credentials["aws_secret_access_key"],
        endpoint_url=resolved_endpoint,
    )

    candidate_prefixes = [
        f"var/lib/pgbackrest/backup/{resolved_stanza}/{resolved_label}/",
        f"backup/{resolved_stanza}/{resolved_label}/",
    ]
    primary_prefix = candidate_prefixes[0]
    listed = {"count": 0, "items": [], "prefix": candidate_prefixes[0]}
    for prefix in candidate_prefixes:
        listed = list_objects(s3, resolved_bucket, prefix, limit=max_scan_keys)
        if listed["count"] > 0:
            break
    if listed["count"] == 0:
        # Fallback scan for unusual key layout while keeping bounded cost.
        for fallback_prefix in ("var/lib/pgbackrest/backup/", "backup/"):
            fallback = list_objects(s3, resolved_bucket, fallback_prefix, limit=max_scan_keys)
            filtered = [item for item in fallback["items"] if resolved_label in str(item.get("key") or "")]
            if filtered:
                listed = {"count": len(filtered), "items": filtered, "prefix": f"{fallback_prefix}*"}
                break

    if listed["count"] == 0:
        return {
            "ok": False,
            "error": (
                f"No S3 objects found for backup label '{resolved_label}' "
                f"in bucket '{resolved_bucket}'."
            ),
            "label": resolved_label,
            "bucket": resolved_bucket,
            "stanza": resolved_stanza,
            "prefix": primary_prefix,
        }

    resolved_prefix = str(listed.get("prefix") or primary_prefix)
    required_keys: list[str] = []
    missing_required: list[str] = []
    if not resolved_prefix.endswith("*"):
        required_suffixes = (
            "backup.manifest",
            "backup.manifest.copy",
            "pg_data/backup_label.gz",
        )
        required_keys = [f"{resolved_prefix}{suffix}" for suffix in required_suffixes]
        for key in required_keys:
            if not object_exists(s3, resolved_bucket, key):
                missing_required.append(key)
        if missing_required:
            return {
                "ok": False,
                "error": "Missing required backup files for the detected label prefix.",
                "label": resolved_label,
                "bucket": resolved_bucket,
                "stanza": resolved_stanza,
                "prefix": resolved_prefix,
                "missing_required_keys": missing_required,
            }

    sample_keys = [str(item.get("key") or "") for item in listed["items"][:8]]
    return {
        "ok": True,
        "label": resolved_label,
        "bucket": resolved_bucket,
        "stanza": resolved_stanza,
        "endpoint": resolved_endpoint,
        "prefix": resolved_prefix,
        "object_count": listed["count"],
        "sample_keys": sample_keys,
        "required_keys": required_keys,
        "credentials_source": credentials.get("source"),
    }


def extract_backup_markers(log_lines: Iterable[str]) -> Dict[str, Optional[str]]:
    """Extract label/bucket/endpoint/stanza values from pgBackRest log lines."""
    label: Optional[str] = None
    bucket: Optional[str] = None
    endpoint: Optional[str] = None
    stanza: Optional[str] = None
    for line in log_lines:
        text = str(line or "")
        label_match = _LABEL_RE.search(text)
        if label_match:
            label = label_match.group(1).strip()
        bucket_match = _BUCKET_RE.search(text)
        if bucket_match:
            bucket = bucket_match.group(1).strip()
        endpoint_match = _ENDPOINT_RE.search(text)
        if endpoint_match:
            endpoint = endpoint_match.group(1).strip()
        stanza_match = _STANZA_RE.search(text)
        if stanza_match:
            stanza = stanza_match.group(1).strip()
    return {
        "label": label,
        "bucket": bucket,
        "endpoint": endpoint,
        "stanza": stanza,
    }


def list_objects(s3: Any, bucket: str, prefix: str, *, limit: int = 5000) -> Dict[str, Any]:
    """List objects under one prefix with a bounded scan limit."""
    paginator = s3.get_paginator("list_objects_v2")
    items: list[Dict[str, Any]] = []
    total = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = str(obj.get("Key") or "")
            total += 1
            if len(items) < 50:
                items.append(
                    {
                        "key": key,
                        "size": int(obj.get("Size") or 0),
                        "etag": str(obj.get("ETag") or ""),
                        "last_modified": str(obj.get("LastModified") or ""),
                    }
                )
            if total >= limit:
                return {"count": total, "items": items, "prefix": prefix}
    return {"count": total, "items": items, "prefix": prefix}


def object_exists(s3: Any, bucket: str, key: str) -> bool:
    """Return True when one object key exists."""
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


def load_s3_credentials(
    *,
    config_path: Optional[Path] = None,
    monocorpus_repo_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load S3 credentials from local Manzara or monocorpus config files."""
    for path in _config_candidates(
        config_path=config_path,
        monocorpus_repo_path=monocorpus_repo_path,
    ):
        if not path.exists():
            continue
        payload = _read_yaml(path)
        if not isinstance(payload, dict):
            continue
        if _contains_redacted(payload):
            continue
        cloud = ((payload.get("yandex") or {}).get("cloud") or {})
        key_id = str(cloud.get("aws_access_key_id") or "").strip()
        key_secret = str(cloud.get("aws_secret_access_key") or "").strip()
        if key_id and key_secret:
            return {
                "ok": True,
                "aws_access_key_id": key_id,
                "aws_secret_access_key": key_secret,
                "source": str(path),
            }
    return {
        "ok": False,
        "error": "Unable to load S3 credentials from Manzara/monocorpus config files.",
    }


def _config_candidates(
    *,
    config_path: Optional[Path],
    monocorpus_repo_path: Optional[Path],
) -> list[Path]:
    repo_root = Path(__file__).resolve().parents[3]
    env_override = str(os.environ.get("MANZARA_CONFIG_PATH") or "").strip()
    monocorpus_root = (monocorpus_repo_path or DEFAULT_MONOCORPUS_REPO).expanduser()

    paths: list[Path] = []
    if config_path:
        paths.append(config_path.expanduser())
    if env_override:
        paths.append(Path(env_override).expanduser())
    paths.extend(
        [
            repo_root / "config.local.yaml",
            repo_root / "config.yaml",
            monocorpus_root / "config.local.yaml",
            monocorpus_root / "config.yaml",
        ]
    )

    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        marker = str(path)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(path)
    return unique


def _read_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _contains_redacted(node: Any) -> bool:
    if isinstance(node, str):
        return REDACTED_SENTINEL in node
    if isinstance(node, dict):
        return any(_contains_redacted(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_redacted(value) for value in node)
    return False
