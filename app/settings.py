"""Runtime settings for Manzara MVP."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.modules.maintenance.config import (
    MaintenanceSettings,
    load_maintenance_settings,
)


@dataclass(frozen=True)
class Settings:
    """Typed settings with safe defaults for local development."""

    database_url: str
    database_schema: str
    maintenance: MaintenanceSettings
    database_pool_size: int = 4
    postgres_backup_mode: str = "local_pgbackrest"


POSTGRES_BACKUP_MODES = frozenset({"local_pgbackrest", "managed"})
LOCAL_PGBACKREST_TASK_IDS = frozenset(
    {
        "maintenance.pgbackrest_backup_full",
        "maintenance.pgbackrest_backup_incr",
    }
)


def task_is_available(settings: Settings | Any, task_id: str) -> bool:
    """Return whether a task may be exposed or started in this deployment."""
    return not (
        str(getattr(settings, "postgres_backup_mode", "local_pgbackrest")) == "managed"
        and str(task_id) in LOCAL_PGBACKREST_TASK_IDS
    )


def normalize_database_url(value: str) -> str:
    """Normalize provider-style PostgreSQL URLs for every runtime client."""
    text = str(value or "").strip()
    if text.startswith("postgres://"):
        return "postgresql://" + text[len("postgres://") :]
    return text


def _contains_redacted(node: Any) -> bool:
    if isinstance(node, str):
        return "<REDACTED>" in node
    if isinstance(node, dict):
        return any(_contains_redacted(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_redacted(value) for value in node)
    return False


def _load_database_url() -> str:
    env_url = str(os.environ.get("MANZARA_DATABASE_URL") or "").strip()
    if env_url:
        return normalize_database_url(env_url)

    config_override = os.environ.get("MANZARA_CONFIG_PATH")
    candidates: list[Path]
    if config_override:
        candidates = [Path(config_override).expanduser()]
    else:
        candidates = [
            Path("config.local.yaml"),
            Path("config.yaml"),
        ]

    for candidate in candidates:
        if not candidate.exists():
            continue
        data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            continue
        if _contains_redacted(data):
            continue
        db_url = str(data.get("database_url") or "").strip()
        if db_url:
            return normalize_database_url(db_url)

    raise RuntimeError(
        "Database URL is not configured. Set MANZARA_DATABASE_URL or provide an unmasked "
        "database_url in config.local.yaml/config.yaml."
    )


def _load_postgres_backup_mode() -> str:
    value = str(os.environ.get("MANZARA_POSTGRES_BACKUP_MODE") or "").strip()
    if not value:
        config_override = os.environ.get("MANZARA_CONFIG_PATH")
        candidates = (
            [Path(config_override).expanduser()]
            if config_override
            else [Path("config.local.yaml"), Path("config.yaml")]
        )
        for candidate in candidates:
            if not candidate.exists():
                continue
            data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                value = str(data.get("postgres_backup_mode") or "").strip()
            if value:
                break
    value = value or "local_pgbackrest"
    if value not in POSTGRES_BACKUP_MODES:
        allowed = ", ".join(sorted(POSTGRES_BACKUP_MODES))
        raise RuntimeError(
            f"MANZARA_POSTGRES_BACKUP_MODE must be one of: {allowed}"
        )
    return value


def _load_database_pool_size() -> int:
    """Load a conservative per-process PostgreSQL connection bound."""
    raw_value = str(os.environ.get("MANZARA_DB_POOL_SIZE") or "").strip()
    if not raw_value:
        config_override = os.environ.get("MANZARA_CONFIG_PATH")
        candidates = (
            [Path(config_override).expanduser()]
            if config_override
            else [Path("config.local.yaml"), Path("config.yaml")]
        )
        for candidate in candidates:
            if not candidate.exists():
                continue
            data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict) and data.get("database_pool_size") is not None:
                raw_value = str(data["database_pool_size"]).strip()
                break
    raw_value = raw_value or "4"
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("MANZARA_DB_POOL_SIZE must be an integer from 1 to 8") from exc
    if not 1 <= value <= 8:
        raise RuntimeError("MANZARA_DB_POOL_SIZE must be an integer from 1 to 8")
    return value


def load_settings() -> Settings:
    """Load runtime settings from env with practical local defaults."""
    database_url = _load_database_url()
    database_schema = str(os.environ.get("MANZARA_DB_SCHEMA", "monocorpus")).strip() or "monocorpus"
    return Settings(
        database_url=database_url,
        database_schema=database_schema,
        maintenance=load_maintenance_settings(),
        database_pool_size=_load_database_pool_size(),
        postgres_backup_mode=_load_postgres_backup_mode(),
    )
