"""Gemini account/key configuration loader."""

from __future__ import annotations

import hashlib
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
    repo_root = Path(__file__).resolve().parent.parent
    return (
        repo_root / "config.local.yaml",
        repo_root / "config.yaml",
        repo_root / "config.example.yaml",
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
            account_id = str(item.get("account_id") or item.get("name") or f"account-{index + 1}").strip()
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


def _iter_legacy_shape(payload: Dict[str, Any]) -> Iterable[GeminiKey]:
    keys = payload.get("gemini_api_keys")
    if not isinstance(keys, list):
        return []
    rows: List[GeminiKey] = []
    for raw_key in keys:
        key = _clean_key(raw_key)
        if not key:
            continue
        account_id = "default"
        rows.append(
            GeminiKey(
                account_id=account_id,
                key_id=_key_id(account_id, key),
                key_value=key,
                masked_key=_mask_key(key),
            )
        )
    return rows


def load_gemini_keys() -> List[GeminiKey]:
    """Load configured Gemini keys (new account-grouped shape with legacy fallback)."""
    payload = _load_config_payload()
    keys = list(_iter_new_shape(payload))
    if keys:
        return keys
    return list(_iter_legacy_shape(payload))

