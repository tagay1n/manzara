"""Gemini account/key configuration loader."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List

from app.runtime_config import load_runtime_config


_REDACTED_SENTINEL = "<REDACTED>"


@dataclass(frozen=True)
class GeminiKey:
    """One Gemini API key with stable identity and masked display value."""

    account_id: str
    key_id: str
    key_value: str
    masked_key: str
    quota_domain_id: str


@dataclass(frozen=True)
class GeminiRuntimeLimits:
    """Shared request and quota-circuit limits for every Gemini workflow."""

    max_quota_rotations_per_model: int = 3
    generic_429_circuit_breaker_threshold: int = 3
    generic_429_window_seconds: int = 60
    generic_429_pause_seconds: int = 60


def _clean_key(value: Any) -> str:
    key = str(value or "").strip()
    if not key:
        return ""
    if _REDACTED_SENTINEL in key:
        return ""
    return key


def _mask_key(key: str) -> str:
    text = str(key or "")
    if not text:
        return ""
    if len(text) <= 8:
        return f"{text[:2]}***{text[-2:]}"
    return f"{text[:4]}...{text[-4:]}"


def _key_id(account_id: str, key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{account_id}:{digest}"


def _configured_key(account_id: str, raw_value: Any) -> GeminiKey | None:
    quota_domain = ""
    key_value = raw_value
    if isinstance(raw_value, dict):
        key_value = raw_value["api_key"]
        if not isinstance(key_value, str):
            raise ValueError("Gemini api_key must be a string")
        quota_domain = raw_value.get("quota_domain", "")
        if not isinstance(quota_domain, str):
            raise ValueError("Gemini quota_domain must be a string")
        quota_domain = quota_domain.strip()
    key = _clean_key(key_value)
    if not key:
        return None
    key_id = _key_id(account_id, key)
    return GeminiKey(
        account_id=account_id,
        key_id=key_id,
        key_value=key,
        masked_key=_mask_key(key),
        quota_domain_id=quota_domain or key_id,
    )


def load_gemini_keys() -> List[GeminiKey]:
    """Read the account-to-key-list mapping documented in config.example.yaml."""
    gemini = load_runtime_config().get("gemini", {})
    if not isinstance(gemini, dict):
        raise ValueError("gemini must be a mapping")
    accounts = gemini.get("accounts", {})
    if not isinstance(accounts, dict):
        raise ValueError("gemini.accounts must map account IDs to key lists")
    rows = []
    for account_id, keys in accounts.items():
        if not isinstance(account_id, str) or not account_id.strip() or not isinstance(keys, list):
            raise ValueError("gemini.accounts requires nonblank string IDs and key lists")
        for raw_key in keys:
            if not isinstance(raw_key, (str, dict)):
                raise ValueError("Gemini keys must be strings or {api_key, quota_domain} mappings")
            if isinstance(raw_key, dict) and ("api_key" not in raw_key or set(raw_key) - {"api_key", "quota_domain"}):
                raise ValueError("Gemini key mappings support only api_key and quota_domain")
            configured = _configured_key(account_id.strip(), raw_key)
            if configured is not None:
                rows.append(configured)
    return rows


def _positive_runtime_integer(
    runtime: Dict[str, Any], field: str, default: int
) -> int:
    value = runtime.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"gemini.runtime.{field} must be a positive integer")
    return value


def load_gemini_runtime_limits() -> GeminiRuntimeLimits:
    """Load strict shared limits, retaining safe defaults when omitted."""
    payload = load_runtime_config()
    gemini = payload.get("gemini")
    raw_runtime = gemini.get("runtime") if isinstance(gemini, dict) else None
    if raw_runtime is None:
        runtime: Dict[str, Any] = {}
    elif isinstance(raw_runtime, dict):
        runtime = raw_runtime
    else:
        raise ValueError("gemini.runtime must be a mapping")
    defaults = GeminiRuntimeLimits()
    return GeminiRuntimeLimits(
        max_quota_rotations_per_model=_positive_runtime_integer(
            runtime,
            "max_quota_rotations_per_model",
            defaults.max_quota_rotations_per_model,
        ),
        generic_429_circuit_breaker_threshold=_positive_runtime_integer(
            runtime,
            "generic_429_circuit_breaker_threshold",
            defaults.generic_429_circuit_breaker_threshold,
        ),
        generic_429_window_seconds=_positive_runtime_integer(
            runtime,
            "generic_429_window_seconds",
            defaults.generic_429_window_seconds,
        ),
        generic_429_pause_seconds=_positive_runtime_integer(
            runtime,
            "generic_429_pause_seconds",
            defaults.generic_429_pause_seconds,
        ),
    )


def load_configured_gemini_model_names() -> List[str]:
    """Return the shared configured runtime model pool without defaults."""
    payload = load_runtime_config()
    gemini = payload.get("gemini")
    raw_models = gemini.get("model_pool") if isinstance(gemini, dict) else None
    if not isinstance(raw_models, list):
        return []
    if any(not isinstance(value, str) or not value.strip() for value in raw_models):
        raise ValueError("gemini.model_pool requires nonblank model names")
    models = [value.strip() for value in raw_models]
    return list(dict.fromkeys(value for value in models if value))


def load_required_gemini_model_pool() -> List[str]:
    """Load the one shared ordered model pool without implicit defaults."""
    models = load_configured_gemini_model_names()
    if not models:
        raise RuntimeError("gemini.model_pool is required and must not be empty")
    return models
