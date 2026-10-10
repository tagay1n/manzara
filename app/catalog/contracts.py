"""Strict, flow-independent catalog value validation."""

from typing import Any
import re


class CatalogConflict(Exception):
    """The reviewed revision no longer describes the current record."""


class CatalogNotFound(Exception):
    """The requested catalog record does not exist."""


def document_md5(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("md5 must be 32 lowercase hexadecimal characters")
    return value


def integer(value: Any, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def nonblank(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonblank text")
    return value.strip()
