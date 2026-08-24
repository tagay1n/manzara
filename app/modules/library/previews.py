"""Library PDF preview domain contracts and read-side helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote


PREVIEW_RECIPE_VERSION = "webp-v1"
PREVIEW_VARIANTS = ("small", "large")
PREVIEW_ROLE_ORDER = ("first", "second", "last")


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


def derive_preview_status(page_count: int, verified_objects: int) -> str:
    """Derive completeness from the count of verified deterministic objects."""
    selected = select_preview_pages(page_count)
    expected = len(selected) * len(PREVIEW_VARIANTS)
    present = max(0, int(verified_objects))
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
    previews: list[dict[str, Any]] = []
    if page_count and str(row.get("status") or "") == "ready":
        for page in select_preview_pages(page_count):
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
        "status": str(row.get("status") or "pending"),
        "source_page_count": page_count,
        "expected_preview_count": len(select_preview_pages(page_count)) if page_count else None,
        "preview_count": len(previews),
        "previews": previews,
    }


__all__ = [
    "PREVIEW_RECIPE_VERSION",
    "PREVIEW_ROLE_ORDER",
    "PREVIEW_VARIANTS",
    "PreviewPage",
    "build_preview_api_payload",
    "derive_preview_status",
    "preview_object_key",
    "select_preview_pages",
]
