"""Library PDF preview domain contracts and read-side helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import quote


PREVIEW_RECIPE_VERSION = "webp-v2"
PREVIEW_VARIANTS = ("small", "large")
PREVIEW_ROLE_ORDER = ("first", "second", "last")
PREVIEW_EDGE_SEARCH_LIMIT = 3
_ROLE_ALIASES = {"first": "1", "second": "2", "last": "l"}


@dataclass(frozen=True)
class PreviewPage:
    """One semantic PDF page selected for preview generation."""

    role: str
    page_number: int
    object_alias: str


def select_preview_pages(page_count: int) -> list[PreviewPage]:
    """Select distinct first/second/last pages for a PDF."""
    count = int(page_count)
    if count < 1:
        raise ValueError("PDF must contain at least one page")
    if count == 1:
        return [PreviewPage("first", 1, "1")]
    if count == 2:
        return [PreviewPage("first", 1, "1"), PreviewPage("last", 2, "l")]
    return [
        PreviewPage("first", 1, "1"),
        PreviewPage("second", 2, "2"),
        PreviewPage("last", count, "l"),
    ]


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


def preview_pages_from_row(row: Mapping[str, Any]) -> list[PreviewPage]:
    """Decode persisted semantic page roles in public display order."""
    selected: list[PreviewPage] = []
    for role in PREVIEW_ROLE_ORDER:
        value = row.get(f"{role}_preview_page")
        if value is None:
            continue
        page_number = int(value)
        if page_number > 0:
            selected.append(PreviewPage(role, page_number, _ROLE_ALIASES[role]))
    return selected


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


def derive_preview_status(selected_page_count: int, verified_objects: int) -> str:
    """Derive completeness from selected pages and verified deterministic objects."""
    expected = max(0, int(selected_page_count)) * len(PREVIEW_VARIANTS)
    present = max(0, int(verified_objects))
    if expected == 0:
        return "ready"
    if present == expected:
        return "ready"
    if present > 0:
        return "partial"
    return "failed"


def _public_object_url(endpoint_url: str, bucket: str, key: str) -> str:
    return (
        f"{str(endpoint_url).rstrip('/')}/{quote(str(bucket), safe='')}/"
        f"{quote(str(key), safe='/')}"
    )


def build_preview_api_payload(
    row: Mapping[str, Any], *, bucket: str, endpoint_url: str
) -> dict[str, Any]:
    """Build deterministic public preview URLs for one ready document."""
    page_count_value = row.get("source_page_count")
    page_count = int(page_count_value) if page_count_value is not None else None
    current_recipe = str(row.get("recipe_version") or "") == PREVIEW_RECIPE_VERSION
    status = str(row.get("status") or "pending") if current_recipe else "pending"
    selected_pages = preview_pages_from_row(row) if current_recipe else []
    previews: list[dict[str, Any]] = []
    if status == "ready":
        for page in selected_pages:
            variants: dict[str, Any] = {}
            for variant in PREVIEW_VARIANTS:
                key = preview_object_key(str(row.get("md5") or ""), page.object_alias, variant)
                variants[variant] = {
                    "url": _public_object_url(endpoint_url, bucket, key),
                }
            previews.append(
                {
                    "role": page.role,
                    "page_number": page.page_number,
                    "variants": variants,
                }
            )

    return {
        "md5": str(row.get("md5") or ""),
        "status": status,
        "source_page_count": page_count,
        "expected_preview_count": (
            len(selected_pages)
            if current_recipe and (status == "ready" or selected_pages)
            else None
        ),
        "preview_count": len(previews),
        "previews": previews,
    }


__all__ = [
    "PREVIEW_RECIPE_VERSION",
    "PREVIEW_EDGE_SEARCH_LIMIT",
    "PREVIEW_ROLE_ORDER",
    "PREVIEW_VARIANTS",
    "PreviewPage",
    "build_preview_api_payload",
    "derive_preview_status",
    "preview_object_key",
    "preview_pages_from_row",
    "select_informative_preview_pages",
    "select_preview_pages",
]
