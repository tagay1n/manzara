"""Gemini account/key configuration loader."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

import yaml


_REDACTED_SENTINEL = "<REDACTED>"
DEFAULT_GEMINI_MODELS: Dict[str, str] = {
    "library_meta_evaluate": "gemini-3-flash-preview",
    "library_normalization": "gemini-2.5-flash",
}
DEFAULT_GEMINI_MODEL_POOLS: Dict[str, List[str]] = {
    "library_collection_validation": [
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3-flash-preview",
    ],
}


@dataclass(frozen=True)
class GeminiKey:
    """One Gemini API key with stable identity and masked display value."""

    account_id: str
    key_id: str
    key_value: str
    masked_key: str


def _candidate_config_paths() -> Sequence[Path]:
    env_override = str(os.environ.get("MANZARA_CONFIG_PATH") or "").strip()
    if env_override:
        return (Path(env_override).expanduser(),)
    repo_root = Path(__file__).resolve().parent.parent
    return (
        repo_root / "config.local.yaml",
        repo_root / "config.yaml",
    )


def _load_config_payload() -> Dict[str, Any]:
    for path in _candidate_config_paths():
        if not path.exists():
            continue
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(payload, dict):
            return payload
    return {}


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
    clean_account = str(account_id or "default").strip() or "default"
    return f"{clean_account}:{digest}"


def _iter_new_shape(payload: Dict[str, Any]) -> Iterable[GeminiKey]:
    gemini = payload.get("gemini")
    if not isinstance(gemini, dict):
        return []
    accounts = gemini.get("accounts")
    if accounts is None:
        return []

    rows: List[GeminiKey] = []
    if isinstance(accounts, list):
        for index, item in enumerate(accounts):
            if not isinstance(item, dict):
                continue
            account_id = str(
                item.get("account_id") or item.get("name") or f"account-{index + 1}"
            ).strip()
            account_id = account_id or f"account-{index + 1}"
            keys = item.get("keys")
            if not isinstance(keys, list):
                continue
            for raw_key in keys:
                key = _clean_key(raw_key)
                if not key:
                    continue
                rows.append(
                    GeminiKey(
                        account_id=account_id,
                        key_id=_key_id(account_id, key),
                        key_value=key,
                        masked_key=_mask_key(key),
                    )
                )
        return rows

    if isinstance(accounts, dict):
        for raw_account_id, keys in accounts.items():
            account_id = str(raw_account_id or "").strip() or "default"
            if not isinstance(keys, list):
                continue
            for raw_key in keys:
                key = _clean_key(raw_key)
                if not key:
                    continue
                rows.append(
                    GeminiKey(
                        account_id=account_id,
                        key_id=_key_id(account_id, key),
                        key_value=key,
                        masked_key=_mask_key(key),
                    )
                )
        return rows

    return []


def load_gemini_keys() -> List[GeminiKey]:
    """Load configured Gemini keys from the account-grouped config shape."""
    return list(_iter_new_shape(_load_config_payload()))


def load_gemini_models() -> Dict[str, str]:
    """Load Gemini model aliases used by task logic."""
    payload = _load_config_payload()
    overrides: Dict[str, str] = {}
    gemini = payload.get("gemini")
    if isinstance(gemini, dict):
        models = gemini.get("models")
        if isinstance(models, dict):
            for raw_alias, raw_model in models.items():
                alias = str(raw_alias or "").strip()
                model = str(raw_model or "").strip()
                if alias and model:
                    overrides[alias] = model
    return {
        **DEFAULT_GEMINI_MODELS,
        **overrides,
    }


def load_gemini_model_pools() -> Dict[str, List[str]]:
    """Load ordered model pools used by load-balanced Gemini workflows."""
    payload = _load_config_payload()
    pools = {key: list(values) for key, values in DEFAULT_GEMINI_MODEL_POOLS.items()}
    gemini = payload.get("gemini")
    configured = gemini.get("model_pools") if isinstance(gemini, dict) else None
    if not isinstance(configured, dict):
        return pools
    for raw_alias, raw_models in configured.items():
        alias = str(raw_alias or "").strip()
        if not alias or not isinstance(raw_models, list):
            continue
        models = [str(value or "").strip() for value in raw_models]
        models = list(dict.fromkeys(value for value in models if value))
        if models:
            pools[alias] = models
    return pools
