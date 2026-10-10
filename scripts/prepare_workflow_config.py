"""Combine Actions-only YAML defaults and existing credentials into private YAML."""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runtime_config import RuntimeConfigLoader, required_text, required_value


def _mapping(contents: bytes, label: str) -> dict:
    try:
        payload = yaml.load(contents, Loader=RuntimeConfigLoader)
    except (yaml.YAMLError, UnicodeError):
        raise ValueError(
            f"{label} must be valid UTF-8 YAML (values suppressed)"
        ) from None
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a YAML mapping")
    return payload


def _merge(target: dict, supplied: dict) -> None:
    for key, value in supplied.items():
        if isinstance(target.get(key), dict) and isinstance(value, dict):
            _merge(target[key], value)
        else:
            target[key] = value


def _environment_text(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or "<REDACTED>" in value or "\n" in value or "\r" in value:
        raise ValueError(f"Configure an unmasked single-line value for {name}")
    return value


def _workflow_payload(profile: str) -> dict:
    defaults = (
        Path(__file__).resolve().parents[1] / ".github/config" / f"{profile}.yaml"
    )
    payload = _mapping(defaults.read_bytes(), "Workflow defaults")
    if profile == "maintenance":
        runner_paths = {
            field: required_text(payload, field)
            for field in ("artifacts_root", "local_state_path")
        }
        cache_path = required_text(payload, "documents", "cache_path")
        supplied = _mapping(
            _decode(
                _environment_text("MANZARA_MAINTENANCE_CONFIG_BASE64"),
                "MANZARA_MAINTENANCE_CONFIG_BASE64",
            ),
            "Maintenance configuration",
        )
        # Ignore unrelated sections from older full application config secrets.
        allowed = set(payload) | {"yandex", "encryption_key"}
        _merge(
            payload, {key: value for key, value in supplied.items() if key in allowed}
        )
        for field in (
            "endpoint_url",
            "region_name",
            "access_key_id",
            "secret_access_key",
        ):
            required_text(payload, "documents", "primary_storage", field)
        for field in (
            "public",
            "private",
            "book_previews",
            "content",
            "content_images",
        ):
            required_text(payload, "documents", "primary_storage", "bucket", field)
        for field in ("source_path", "restricted_path", "filtered_out_path"):
            required_text(payload, "yandex", "disk", "documents", field)
        required_text(payload, "yandex", "disk", "oauth_token")
        required_text(payload, "encryption_key")
        # These are runner paths, not paths copied from a developer's machine.
        payload.update(runner_paths)
        payload["documents"]["cache_path"] = cache_path
    if profile != "link-checker":
        database_url = _environment_text("MANZARA_DATABASE_URL")
        if not database_url.startswith(
            (
                "postgres://",
                "postgresql://",
                "postgresql+psycopg://",
                "postgresql+psycopg2://",
            )
        ):
            raise ValueError("MANZARA_DATABASE_URL must be a PostgreSQL URL")
        payload["database_url"] = database_url
        encoded_ca = os.environ.get("MANZARA_AIVEN_CA_CERT_BASE64", "").strip()
        if profile == "backup":
            encoded_ca = _environment_text("MANZARA_AIVEN_CA_CERT_BASE64")
            for field, suffix in (
                ("endpoint_url", "ENDPOINT"),
                ("region_name", "REGION"),
                ("bucket", "BUCKET"),
                ("access_key_id", "ACCESS_KEY_ID"),
                ("secret_access_key", "SECRET_ACCESS_KEY"),
            ):
                payload["backup"][field] = _environment_text(
                    f"MANZARA_LOGICAL_BACKUP_S3_{suffix}"
                )
        payload["database_ca_certificate_base64"] = encoded_ca or None
    return payload


def _decode(value: str, field: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{field} must be valid base64") from None


def _private_file(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(contents)


def prepare(profile: str) -> None:
    target = Path(os.environ["MANZARA_CONFIG_PATH"])
    payload = _workflow_payload(profile)

    # Only machine-local paths support this explicit runner placeholder.
    for mapping, field in (
        (payload, "artifacts_root"),
        (payload, "local_state_path"),
        (payload.get("documents"), "cache_path"),
    ):
        if not isinstance(mapping, dict) or field not in mapping:
            continue
        value = required_text(mapping, field)
        if "${RUNNER_TEMP}" in value:
            value = value.replace("${RUNNER_TEMP}", os.environ["RUNNER_TEMP"])
        if "$" in value:
            raise ValueError(f"Unsupported path placeholder: {field}")
        mapping[field] = value

    certificate = (
        required_value(payload, "database_ca_certificate_base64")
        if "database_url" in payload
        else None
    )
    if certificate is not None:
        if not isinstance(certificate, str):
            raise ValueError("database_ca_certificate_base64 must be a string or null")
        pem = _decode(certificate, "database_ca_certificate_base64")
        if b"-----BEGIN CERTIFICATE-----" not in pem:
            raise ValueError(
                "database_ca_certificate_base64 must contain a PEM certificate"
            )
        ca_path = target.parent / "database-ca.pem"
        _private_file(ca_path, pem)
        parts = urlsplit(required_text(payload, "database_url"))
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != "sslrootcert"
        ]
        payload["database_url"] = urlunsplit(
            parts._replace(query=urlencode([*query, ("sslrootcert", str(ca_path))]))
        )

    _private_file(
        target,
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8"),
    )
    print("Prepared private runtime YAML", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "profile", choices=("maintenance", "export", "backup", "link-checker")
    )
    args = parser.parse_args()
    try:
        prepare(args.profile)
    except (KeyError, OSError, ValueError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            else "Could not materialize private workflow configuration"
        )
        print(f"::error::{message}", flush=True)
        raise SystemExit(1) from None
