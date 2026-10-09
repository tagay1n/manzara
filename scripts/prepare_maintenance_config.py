"""Materialize maintenance-only Actions credentials without printing their values."""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml


def _decode(value: str, name: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{name} must be valid base64") from None


def _mapping(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a YAML mapping")
    return value


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "<REDACTED>" in value:
        raise ValueError(f"Configure an unmasked string for {name}")
    return value.strip()


def _mask(value: str) -> None:
    safe = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::add-mask::{safe}", flush=True)


def _private_file(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(contents)


def prepare() -> None:
    root = Path(os.environ["MANZARA_ARTIFACTS_ROOT"])
    target = Path(os.environ["MANZARA_CONFIG_PATH"])
    database_url = _text(os.environ.get("MANZARA_DATABASE_URL"), "MANZARA_DATABASE_URL")
    if not database_url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://", "postgresql+psycopg2://")):
        raise ValueError("MANZARA_DATABASE_URL must be a PostgreSQL URL")
    if "\n" in database_url or "\r" in database_url:
        raise ValueError("MANZARA_DATABASE_URL must be a single line")
    encoded = _text(os.environ.get("MANZARA_MAINTENANCE_CONFIG_BASE64"), "MANZARA_MAINTENANCE_CONFIG_BASE64")
    try:
        config = _mapping(yaml.safe_load(_decode(encoded, "MANZARA_MAINTENANCE_CONFIG_BASE64")), "config")
    except (yaml.YAMLError, UnicodeError):
        raise ValueError("Maintenance configuration must be valid UTF-8 YAML") from None
    documents = _mapping(config.get("documents"), "documents")
    primary = _mapping(documents.get("primary_storage"), "documents.primary_storage")
    buckets = _mapping(primary.get("bucket"), "documents.primary_storage.bucket")
    yandex = _mapping(config.get("yandex"), "yandex")
    disk = _mapping(yandex.get("disk"), "yandex.disk")
    paths = _mapping(disk.get("documents"), "yandex.disk.documents")
    primary_values = {key: _text(primary.get(key), f"documents.primary_storage.{key}")
                      for key in ("endpoint_url", "region_name", "access_key_id", "secret_access_key")}
    bucket_values = {key: _text(buckets.get(key), f"documents.primary_storage.bucket.{key}")
                     for key in ("public", "private", "book_previews", "content", "content_images")}
    path_values = {key: _text(paths.get(key), f"yandex.disk.documents.{key}")
                   for key in ("source_path", "restricted_path", "filtered_out_path")}
    token = _text(disk.get("oauth_token"), "yandex.disk.oauth_token")
    encryption_key = _text(config.get("encryption_key"), "encryption_key")
    for secret in (primary_values["access_key_id"], primary_values["secret_access_key"], token, encryption_key):
        _mask(secret)
    payload = {
        "documents": {"cache_path": str(root / "cache/source-documents"), "cache_max_gib": 1,
                      "primary_storage": {**primary_values, "bucket": bucket_values}},
        "yandex": {"disk": {"oauth_token": token, "documents": path_values}},
        "encryption_key": encryption_key,
    }
    _private_file(target, yaml.safe_dump(payload, allow_unicode=True).encode("utf-8"))
    encoded_ca = os.environ.get("MANZARA_AIVEN_CA_CERT_BASE64", "").strip()
    if encoded_ca:
        certificate = _decode(encoded_ca, "MANZARA_AIVEN_CA_CERT_BASE64")
        if b"-----BEGIN CERTIFICATE-----" not in certificate:
            raise ValueError("MANZARA_AIVEN_CA_CERT_BASE64 must contain a PEM certificate")
        ca_path = target.parent / "aiven-ca.pem"
        _private_file(ca_path, certificate)
        parts = urlsplit(database_url)
        query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "sslrootcert"]
        database_url = urlunsplit(parts._replace(query=urlencode([*query, ("sslrootcert", str(ca_path))])))
        _mask(database_url)
        with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as handle:
            handle.write(f"MANZARA_DATABASE_URL={database_url}\n")


if __name__ == "__main__":
    try:
        prepare()
    except (KeyError, OSError, ValueError) as exc:
        # Do not expose YAML parser context, decoded values, or provider URLs.
        message = str(exc) if isinstance(exc, ValueError) else "Could not materialize private maintenance configuration"
        print(f"::error::{message}", flush=True)
        raise SystemExit(1) from None
