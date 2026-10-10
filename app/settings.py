"""Required runtime settings from the single local YAML configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.runtime_config import load_runtime_config, required_integer, required_text


@dataclass(frozen=True)
class Settings:
    database_url: str
    database_schema: str
    database_pool_size: int
    local_state_path: Path


def normalize_database_url(value: str) -> str:
    text = value.strip()
    if text.startswith("postgres://"):
        return "postgresql://" + text[len("postgres://") :]
    return text


def configured_schema(field: str) -> str:
    value = required_text(load_runtime_config(), field)
    if len(value) > 63 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Invalid schema identifier: {field}")
    return value


def load_settings() -> Settings:
    payload = load_runtime_config()
    return Settings(
        database_url=normalize_database_url(required_text(payload, "database_url")),
        database_schema=configured_schema("database_schema"),
        database_pool_size=required_integer(payload, "database_pool_size", maximum=8),
        local_state_path=Path(required_text(payload, "local_state_path")).expanduser(),
    )
