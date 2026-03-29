"""S3 verification helpers for pgBackRest backup runs."""

from __future__ import annotations

import os
import re
import time
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

DEFAULT_PGBACKREST_S3_BUCKET = "tt-monocorpus-postgres-backups"
DEFAULT_PGBACKREST_STANZA = "monocorpus"
DEFAULT_VERIFY_WAIT_SECONDS = 120
DEFAULT_VERIFY_POLL_SECONDS = 5.0

PGBACKREST_BACKUP_ROOTS = (
    "var/lib/pgbackrest/backup/{stanza}/",
    "backup/{stanza}/",
)

REQUIRED_BACKUP_FILES = (
    "backup.manifest",
    "pg_data/backup_label.gz",
)


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


def capture_pgbackrest_s3_state(
    *,
    command_value: Optional[str] = None,
    bucket: Optional[str] = None,
    stanza: Optional[str] = None,
    endpoint: Optional[str] = None,
    config_path: Optional[Path] = None,
    monocorpus_repo_path: Optional[Path] = None,
    max_scan_labels: int = 5000,
) -> Dict[str, Any]:
    """Capture current pgBackRest labels available in S3 for one stanza."""
    resolved_stanza = str(stanza or _extract_stanza_from_command(command_value) or "").strip()
    if not resolved_stanza:
        resolved_stanza = DEFAULT_PGBACKREST_STANZA
    resolved_bucket = str(
        bucket
        or os.environ.get("PG_BACKREST_S3_BUCKET")
        or os.environ.get("MANZARA_PGBACKREST_S3_BUCKET")
        or DEFAULT_PGBACKREST_S3_BUCKET
    ).strip()
    resolved_endpoint = str(
        endpoint
        or os.environ.get("PG_BACKREST_S3_ENDPOINT")
        or os.environ.get("MANZARA_PGBACKREST_S3_ENDPOINT")
        or DEFAULT_ENDPOINT
    ).strip()

    if not resolved_bucket:
        return {
            "ok": False,
            "error": "S3 bucket is not configured for pgBackRest verification.",
        }
    if not resolved_stanza:
        return {
            "ok": False,
            "error": "pgBackRest stanza is not configured for S3 verification.",
        }

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

    labels: set[str] = set()
    label_prefixes: dict[str, list[str]] = {}
    scanned_roots: list[str] = []
    for root_template in PGBACKREST_BACKUP_ROOTS:
        root = root_template.format(stanza=resolved_stanza)
        scanned_roots.append(root)
        listed = list_child_prefixes(s3, resolved_bucket, root, limit=max_scan_labels)
        for label in listed.get("labels", []):
            safe_label = str(label).strip()
            if not safe_label:
                continue
            labels.add(safe_label)
            label_prefixes.setdefault(safe_label, [])
            label_prefixes[safe_label].append(f"{root}{safe_label}/")

    return {
        "ok": True,
        "bucket": resolved_bucket,
        "stanza": resolved_stanza,
        "endpoint": resolved_endpoint,
        "labels": sorted(labels),
        "label_count": len(labels),
        "roots": scanned_roots,
        "label_prefixes": label_prefixes,
        "credentials_source": credentials.get("source"),
    }


def wait_for_pgbackrest_s3_change(
    *,
    before_state: Dict[str, Any],
    command_value: Optional[str] = None,
    bucket: Optional[str] = None,
    stanza: Optional[str] = None,
    endpoint: Optional[str] = None,
    config_path: Optional[Path] = None,
    monocorpus_repo_path: Optional[Path] = None,
    wait_seconds: int = DEFAULT_VERIFY_WAIT_SECONDS,
    poll_interval_seconds: float = DEFAULT_VERIFY_POLL_SECONDS,
) -> Dict[str, Any]:
    """Poll S3 until new backup label appears and required files are present."""
    if not before_state.get("ok"):
        return {
            "ok": False,
            "error": str(before_state.get("error") or "unable to capture pre-run S3 backup state"),
        }

    resolved_bucket = str(bucket or before_state.get("bucket") or "").strip()
    resolved_stanza = str(stanza or before_state.get("stanza") or "").strip()
    resolved_endpoint = str(endpoint or before_state.get("endpoint") or "").strip()
    labels_before = {str(item).strip() for item in (before_state.get("labels") or []) if str(item).strip()}

    timeout_seconds = max(0, int(wait_seconds))
    sleep_seconds = max(0.1, float(poll_interval_seconds))
    deadline = time.monotonic() + float(timeout_seconds)
    latest_state: Optional[Dict[str, Any]] = None
    attempts = 0

    while True:
        attempts += 1
        current = capture_pgbackrest_s3_state(
            command_value=command_value,
            bucket=resolved_bucket,
            stanza=resolved_stanza,
            endpoint=resolved_endpoint,
            config_path=config_path,
            monocorpus_repo_path=monocorpus_repo_path,
        )
        latest_state = current
        if not current.get("ok"):
            return current

        labels_after = {
            str(item).strip()
            for item in (current.get("labels") or [])
            if str(item).strip()
        }
        labels_added = sorted(labels_after - labels_before)
        if labels_added:
            validated = _validate_new_labels_have_required_files(
                current,
                labels_added,
                config_path=config_path,
                monocorpus_repo_path=monocorpus_repo_path,
            )
            if validated.get("ok"):
                validated["labels_added"] = labels_added
                validated["poll_attempts"] = attempts
                return validated
            return validated

        now = time.monotonic()
        if now >= deadline:
            return {
                "ok": False,
                "error": (
                    f"No new backup label appeared in S3 within {timeout_seconds}s "
                    f"(stanza={resolved_stanza}, bucket={resolved_bucket})."
                ),
                "bucket": resolved_bucket,
                "stanza": resolved_stanza,
                "endpoint": resolved_endpoint,
                "labels_before_count": len(labels_before),
                "labels_after_count": len(labels_after),
                "poll_attempts": attempts,
            }
        time.sleep(sleep_seconds)


def list_child_prefixes(s3: Any, bucket: str, parent_prefix: str, *, limit: int = 5000) -> Dict[str, Any]:
    """List one-level child prefixes under parent prefix and return label names."""
    paginator = s3.get_paginator("list_objects_v2")
    labels: set[str] = set()
    scanned = 0

    for page in paginator.paginate(Bucket=bucket, Prefix=parent_prefix, Delimiter="/"):
        for pref in page.get("CommonPrefixes", []):
            prefix = str(pref.get("Prefix") or "")
            label = _extract_label_from_prefix(parent_prefix, prefix)
            if label:
                labels.add(label)
            scanned += 1
            if scanned >= limit:
                return {"labels": sorted(labels), "count": len(labels), "prefix": parent_prefix}

        # Some S3-compatible providers may not always return CommonPrefixes as expected.
        # Fall back to deriving labels from object keys in the same response page.
        for obj in page.get("Contents", []):
            key = str(obj.get("Key") or "")
            label = _extract_label_from_key(parent_prefix, key)
            if label:
                labels.add(label)
            scanned += 1
            if scanned >= limit:
                return {"labels": sorted(labels), "count": len(labels), "prefix": parent_prefix}

    return {"labels": sorted(labels), "count": len(labels), "prefix": parent_prefix}


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


def _validate_new_labels_have_required_files(
    state: Dict[str, Any],
    labels_added: list[str],
    *,
    config_path: Optional[Path] = None,
    monocorpus_repo_path: Optional[Path] = None,
) -> Dict[str, Any]:
    bucket = str(state.get("bucket") or "").strip()
    endpoint = str(state.get("endpoint") or "").strip()
    stanza = str(state.get("stanza") or "").strip()
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
        endpoint_url=endpoint or DEFAULT_ENDPOINT,
    )

    label_prefixes = state.get("label_prefixes") or {}
    missing_by_label: dict[str, list[str]] = {}
    for label in labels_added:
        prefixes = list(label_prefixes.get(label) or [])
        if not prefixes:
            prefixes = [
                root_template.format(stanza=stanza) + f"{label}/"
                for root_template in PGBACKREST_BACKUP_ROOTS
            ]

        for prefix in prefixes:
            required_keys = [f"{prefix}{suffix}" for suffix in REQUIRED_BACKUP_FILES]
            missing = [key for key in required_keys if not object_exists(s3, bucket, key)]
            if not missing:
                sample = list_objects(s3, bucket, prefix, limit=20)
                sample_keys = [str(item.get("key") or "") for item in sample.get("items", [])[:8]]
                return {
                    "ok": True,
                    "bucket": bucket,
                    "stanza": stanza,
                    "endpoint": endpoint or DEFAULT_ENDPOINT,
                    "label": label,
                    "prefix": prefix,
                    "object_count": int(sample.get("count") or 0),
                    "sample_keys": sample_keys,
                    "required_keys": required_keys,
                    "credentials_source": credentials.get("source"),
                }
            missing_by_label[label] = missing

    return {
        "ok": False,
        "error": "New backup label detected, but required files are missing in S3.",
        "bucket": bucket,
        "stanza": stanza,
        "labels_added": labels_added,
        "missing_required_keys": missing_by_label,
    }


def _extract_stanza_from_command(command_value: Optional[str]) -> Optional[str]:
    text = str(command_value or "")
    if not text.strip():
        return None
    match = _STANZA_RE.search(text)
    if not match:
        return None
    value = str(match.group(1) or "").strip()
    return value or None


def _extract_label_from_prefix(parent_prefix: str, child_prefix: str) -> Optional[str]:
    if not child_prefix.startswith(parent_prefix):
        return None
    remainder = child_prefix[len(parent_prefix):].strip("/")
    if not remainder:
        return None
    return remainder.split("/", 1)[0] or None


def _extract_label_from_key(parent_prefix: str, key: str) -> Optional[str]:
    if not key.startswith(parent_prefix):
        return None
    remainder = key[len(parent_prefix):]
    if not remainder:
        return None
    return remainder.split("/", 1)[0] or None


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
