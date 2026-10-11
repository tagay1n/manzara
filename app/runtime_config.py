"""Shared loader for local runtime YAML configuration."""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
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
    path = runtime_config_path()
    try:
        stat = path.stat()
        payload = deepcopy(
            _read_config(path, (stat.st_mtime_ns, stat.st_size, stat.st_ino))
        )
    except OSError:
        raise RuntimeError(
            "Cannot read runtime config; provide config.yaml or MANZARA_CONFIG_PATH"
        ) from None
    return _validate_config(payload)


def runtime_config_path() -> Path:
    """Resolve the single selected private YAML, including symlink targets."""
    override = str(os.environ.get("MANZARA_CONFIG_PATH") or "").strip()
    repo_root = Path(__file__).resolve().parent.parent
    path = (Path(override).expanduser() if override else repo_root / "config.yaml").resolve()
    if path == (repo_root / "config.example.yaml").resolve():
        raise ValueError(
            "config.example.yaml is documentation, not runtime configuration"
        )
    return path


def _validate_config(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Runtime configuration must be a YAML mapping")
    if _contains_redacted(payload):
        raise ValueError("Replace masked runtime configuration values")
    return payload


def _parse_config(source: str) -> dict:
    try:
        return _validate_config(yaml.load(source, Loader=RuntimeConfigLoader))
    except yaml.YAMLError:
        raise ValueError("Runtime configuration must be valid UTF-8 YAML (values suppressed)") from None


def runtime_config_snapshot() -> tuple[Path, str, dict]:
    """Read editable configuration and an opaque revision without logging values."""
    path = runtime_config_path()
    try:
        source = path.read_bytes()
        return path, hashlib.sha256(source).hexdigest(), _parse_config(source.decode("utf-8"))
    except UnicodeError:
        raise ValueError("Runtime configuration must be valid UTF-8 YAML (values suppressed)") from None
    except OSError:
        raise RuntimeError("Cannot read the selected private configuration") from None


def _config_node(root, keys):
    node = root
    for key in keys:
        if not isinstance(node, yaml.MappingNode):
            raise ValueError(f"Config setting is not a mapping: {'.'.join(keys)}")
        node = next((value for name, value in node.value if name.value == key), None)
        if node is None:
            raise ValueError(f"Missing required config value: {'.'.join(keys)}")
    return node


def save_runtime_config(*, path: Path, revision: str, changes: dict[tuple[str, ...], Any]) -> None:
    """Atomically replace declared YAML values, preserving other bytes and comments."""
    if path != runtime_config_path():
        raise ValueError("Selected configuration changed; reopen /settings")
    temporary = None
    try:
        original = path.read_bytes()
        if hashlib.sha256(original).hexdigest() != revision:
            raise ValueError("Configuration changed outside this form; reopen /settings")
        source = original.decode("utf-8")
        payload = _parse_config(source)
        root = yaml.compose(source, Loader=RuntimeConfigLoader)
        edits = []
        for keys, value in changes.items():
            if value is not None and type(value) is not int:
                raise ValueError(f"Cannot edit config value: {'.'.join(keys)}")
            replacement = "null" if value is None else str(value)
            parent = payload
            for key in keys[:-1]:
                parent = parent[key]
            parent_node = _config_node(root, keys[:-1])
            if not isinstance(parent_node, yaml.MappingNode):
                raise ValueError(f"Config setting is not a mapping: {'.'.join(keys[:-1])}")
            node = next((child for key, child in parent_node.value if key.value == keys[-1]), None)
            if node is not None:
                if not isinstance(node, yaml.ScalarNode):
                    raise ValueError(f"Config setting must be a scalar: {'.'.join(keys)}")
                if node.start_mark.index == node.end_mark.index:
                    replacement = " " + replacement
                edits.append((node.start_mark.index, node.end_mark.index, replacement))
            elif parent_node.flow_style:
                values = {**parent, keys[-1]: value}
                replacement = yaml.safe_dump(values, default_flow_style=True, width=100000).strip()
                edits.append((parent_node.start_mark.index, parent_node.end_mark.index, replacement))
            else:
                last = parent_node.value[-1][1]
                position = source.find("\n", last.end_mark.index)
                prefix = "\n" if position < 0 else ""
                position = len(source) if position < 0 else position + 1
                newline = "\r\n" if "\r\n" in source else "\n"
                replacement = f"{prefix}{' ' * parent_node.start_mark.column}{keys[-1]}: {replacement}{newline}"
                edits.append((position, position, replacement))
            parent[keys[-1]] = value
        for start, end, replacement in sorted(edits, reverse=True):
            source = source[:start] + replacement + source[end:]
        if _parse_config(source) != payload:
            raise ValueError("Cannot preserve configuration structure while saving")
        descriptor, name = tempfile.mkstemp(prefix=".manzara-config-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(source.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(path.stat().st_mode & 0o777)
        if path.read_bytes() != original:
            raise ValueError("Configuration changed while saving; reopen /settings")
        os.replace(temporary, path)
        _read_config.cache_clear()
    except yaml.YAMLError:
        raise ValueError("Cannot preserve YAML structure while saving (values suppressed)") from None
    except (OSError, UnicodeError):
        raise RuntimeError("Cannot save private configuration; check its file and directory permissions") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
