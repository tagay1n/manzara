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
from app.modules.shayan.config import ShayanSettings, load_shayan_settings


@dataclass(frozen=True)
class Settings:
    """Typed settings with safe defaults for local development."""

    database_url: str
    database_schema: str
    shayan: ShayanSettings
    maintenance: MaintenanceSettings


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
        return env_url

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
            return db_url

    raise RuntimeError(
        "Database URL is not configured. Set MANZARA_DATABASE_URL or provide an unmasked "
        "database_url in config.local.yaml/config.yaml."
    )


def load_settings() -> Settings:
    """Load runtime settings from env with practical local defaults."""
    database_url = _load_database_url()
    database_schema = str(os.environ.get("MANZARA_DB_SCHEMA", "monocorpus")).strip() or "monocorpus"
    return Settings(
        database_url=database_url,
        database_schema=database_schema,
        shayan=load_shayan_settings(),
        maintenance=load_maintenance_settings(),
    )
