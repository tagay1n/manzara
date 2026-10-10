"""Materialize explicit workflow YAML without logging configuration values."""

from __future__ import annotations

import base64
import binascii
import os
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runtime_config import RuntimeConfigLoader, required_text, required_value


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


def prepare() -> None:
    target = Path(os.environ["MANZARA_CONFIG_PATH"])
    encoded = os.environ["MANZARA_CONFIG_BASE64"]
    try:
        payload = yaml.load(
            _decode(encoded, "MANZARA_CONFIG_BASE64"), Loader=RuntimeConfigLoader
        )
    except (yaml.YAMLError, UnicodeError):
        raise ValueError(
            "Workflow configuration must be valid UTF-8 YAML (values suppressed)"
        ) from None
    if not isinstance(payload, dict):
        raise ValueError("Workflow configuration must be a YAML mapping")

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
    try:
        prepare()
    except (KeyError, OSError, ValueError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            else "Could not materialize private workflow configuration"
        )
        print(f"::error::{message}", flush=True)
        raise SystemExit(1) from None
