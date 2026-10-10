"""Library PDF preview domain contracts and read-side helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


PREVIEW_RECIPE_VERSION = "webp-v2"
PREVIEW_ROLE_ORDER = ("first", "second", "last")
PREVIEW_EDGE_SEARCH_LIMIT = 3
_ROLE_ALIASES = {"first": "1", "second": "2", "last": "l"}


@dataclass(frozen=True)
class PreviewPage:
    """One semantic PDF page selected for preview generation."""

    role: str
    page_number: int
    object_alias: str


def select_informative_preview_pages(
    page_count: int,
    *,
    is_useful: Callable[[int], bool],
    edge_limit: int = PREVIEW_EDGE_SEARCH_LIMIT,
) -> list[PreviewPage]:
    """Select distinct first/second/last roles from bounded useful edge pages."""
    count = int(page_count)
    if count < 1:
        raise ValueError("PDF must contain at least one page")
    limit = max(1, int(edge_limit))
    front = range(1, min(count, limit) + 1)
    back = range(count, max(0, count - limit), -1)
    decisions: dict[int, bool] = {}

    def useful(page_number: int) -> bool:
        if page_number not in decisions:
            decisions[page_number] = bool(is_useful(page_number))
        return decisions[page_number]

    selected: dict[str, int] = {}
    for page_number in front:
        if useful(page_number):
            selected["first"] = page_number
            break
    used = set(selected.values())
    for page_number in back:
        if page_number not in used and useful(page_number):
            selected["last"] = page_number
            used.add(page_number)
            break
    for page_number in front:
        if page_number not in used and useful(page_number):
            selected["second"] = page_number
            break

    return [
        PreviewPage(role, selected[role], _ROLE_ALIASES[role])
        for role in PREVIEW_ROLE_ORDER
        if role in selected
    ]


def preview_object_key(md5: str, object_alias: str, variant: str) -> str:
    """Build one deterministic compact preview object key."""
    digest = str(md5 or "").strip().lower()
    if len(digest) != 32 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("md5 must be a 32-character lowercase hexadecimal digest")
    alias = str(object_alias or "").strip()
    if alias not in {"1", "2", "l"}:
        raise ValueError(f"Unsupported preview object alias: {alias!r}")
    normalized_variant = str(variant or "").strip().lower()
    suffixes = {"small": "s", "large": "l"}
    if normalized_variant not in suffixes:
        raise ValueError(f"Unsupported preview variant: {normalized_variant!r}")
    filename = f"{alias}{suffixes[normalized_variant]}.webp"
    return f"{digest}/{filename}"


__all__ = [
    "PREVIEW_RECIPE_VERSION",
    "PREVIEW_EDGE_SEARCH_LIMIT",
    "PREVIEW_ROLE_ORDER",
    "PreviewPage",
    "preview_object_key",
    "select_informative_preview_pages",
]
