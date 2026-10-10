"""Shared loader for local runtime YAML configuration."""

from __future__ import annotations

import math
import os
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import yaml


class RuntimeConfigLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently shadowing a setting."""


def _unique_mapping(loader: RuntimeConfigLoader, node: Any, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError:
            raise yaml.YAMLError("Configuration mapping keys must be scalar") from None
        if duplicate:
            raise yaml.YAMLError("Duplicate configuration mapping key")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


RuntimeConfigLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping
)


def _contains_redacted(node: Any) -> bool:
    if isinstance(node, str):
        return "<REDACTED>" in node
    if isinstance(node, dict):
        return any(_contains_redacted(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_redacted(value) for value in node)
    return False


def required_value(payload: Any, *keys: str) -> Any:
    """Read an explicit YAML value, reporting only its configuration path."""
    value = payload
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"Missing required config value: {'.'.join(keys)}")
        value = value[key]
    return value


def required_text(payload: Any, *keys: str) -> str:
    value = required_value(payload, *keys)
    if not isinstance(value, str) or not value.strip() or _contains_redacted(value):
        raise ValueError(f"Configure an unmasked nonblank string for {'.'.join(keys)}")
    return value.strip()


def required_integer(
    payload: Any, *keys: str, minimum: int = 1, maximum: int | None = None
) -> int:
    value = required_value(payload, *keys)
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ValueError(f"Invalid integer config value: {'.'.join(keys)}")
    return value


def config_value(*keys: str) -> Any:
    return required_value(load_runtime_config(), *keys)


def config_text(*keys: str) -> str:
    return required_text(load_runtime_config(), *keys)


def config_integer(*keys: str, minimum: int = 1, maximum: int | None = None) -> int:
    return required_integer(
        load_runtime_config(), *keys, minimum=minimum, maximum=maximum
    )


def config_number(
    *keys: str, minimum: float = 0, maximum: float | None = None
) -> float:
    value = config_value(*keys)
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ValueError(f"Invalid numeric config value: {'.'.join(keys)}")
    return float(value)


def config_text_list(*keys: str) -> list[str]:
    value = config_value(*keys)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ValueError(f"Configure a nonempty string list for {'.'.join(keys)}")
    return [item.strip() for item in value]


@lru_cache(maxsize=4)
def _read_config(path: Path, signature: tuple[int, int, int]) -> Any:
    # Reload on file replacement/edit; never expose parser context containing secrets.
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=RuntimeConfigLoader)
    except (yaml.YAMLError, UnicodeError):
        raise ValueError(
            "Runtime configuration must be valid UTF-8 YAML (values suppressed)"
        ) from None


def load_runtime_config() -> Dict[str, Any]:
    """Load one explicit local YAML file; environment selects only its path."""
    override = str(os.environ.get("MANZARA_CONFIG_PATH") or "").strip()
    repo_root = Path(__file__).resolve().parent.parent
    path = Path(override).expanduser() if override else repo_root / "config.yaml"
    if path.resolve() == (repo_root / "config.example.yaml").resolve():
        raise ValueError(
            "config.example.yaml is documentation, not runtime configuration"
        )
    try:
        stat = path.stat()
        payload = deepcopy(
            _read_config(path, (stat.st_mtime_ns, stat.st_size, stat.st_ino))
        )
    except OSError:
        raise RuntimeError(
            "Cannot read runtime config; provide config.yaml or MANZARA_CONFIG_PATH"
        ) from None
    if not isinstance(payload, dict):
        raise ValueError("Runtime configuration must be a YAML mapping")
    if _contains_redacted(payload):
        raise ValueError("Replace masked runtime configuration values")
    return payload
