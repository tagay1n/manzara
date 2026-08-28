"""Shared schema.org DefinedTerm helpers for Library metadata."""

from __future__ import annotations

from typing import Any, Mapping


def termset_name(value: Any) -> str | None:
    """Read a term-set name from strict or legacy schema.org data."""
    if isinstance(value, Mapping):
        value = value.get("name")
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def defined_term(term_value: str, termset: str) -> dict[str, Any]:
    """Build the canonical JSON-LD representation for a managed term."""
    value_key = "name" if termset.casefold() == "categorypath" else "termCode"
    return {
        "@type": "DefinedTerm",
        value_key: term_value,
        "inDefinedTermSet": {
            "@type": "DefinedTermSet",
            "name": termset,
        },
    }


__all__ = ["defined_term", "termset_name"]
