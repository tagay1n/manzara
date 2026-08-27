"""Gemini account/key configuration loader."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

import yaml


_REDACTED_SENTINEL = "<REDACTED>"


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


def load_configured_gemini_model_names() -> List[str]:
    """Return the shared configured runtime model pool without defaults."""
    payload = _load_config_payload()
    gemini = payload.get("gemini")
    raw_models = gemini.get("model_pool") if isinstance(gemini, dict) else None
    if not isinstance(raw_models, list):
        return []
    models = [str(value or "").strip() for value in raw_models]
    return list(dict.fromkeys(value for value in models if value))


def load_required_gemini_model_pool() -> List[str]:
    """Load the one shared ordered model pool without implicit defaults."""
    models = load_configured_gemini_model_names()
    if not models:
        raise RuntimeError("gemini.model_pool is required and must not be empty")
    return models
