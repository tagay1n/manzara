"""Library PDF preview domain contracts and read-side helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote


PREVIEW_RECIPE_VERSION = "pdf-three-page-webp-v1"
PREVIEW_OBJECT_PREFIX = f"library/{PREVIEW_RECIPE_VERSION}"
PREVIEW_VARIANTS = ("small", "large")
PREVIEW_ROLE_ORDER = ("first", "second", "last")
PUBLIC_S3_ENDPOINT = "https://storage.yandexcloud.net"


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
    return f"{PREVIEW_OBJECT_PREFIX}/{digest[:2]}/{digest}/{filename}"


def _manifest_mapping(manifest: Any) -> dict[str, Any]:
    return dict(manifest) if isinstance(manifest, Mapping) else {}


def derive_preview_status(page_count: int, manifest: Any) -> str:
    """Derive completeness from expected semantic pages and two variants each."""
    selected = select_preview_pages(page_count)
    payload = _manifest_mapping(manifest)
    present = 0
    expected = len(selected) * len(PREVIEW_VARIANTS)
    for page in selected:
        page_data = payload.get(page.role)
        variants = page_data.get("variants") if isinstance(page_data, Mapping) else None
        for variant in PREVIEW_VARIANTS:
            item = variants.get(variant) if isinstance(variants, Mapping) else None
            if isinstance(item, Mapping) and str(item.get("key") or "").strip():
                present += 1
    if present == expected:
        return "ready"
    if present > 0:
        return "partial"
    return "failed"


def _public_object_url(bucket: str, key: str) -> str:
    return f"{PUBLIC_S3_ENDPOINT}/{quote(str(bucket), safe='')}/{quote(str(key), safe='/')}"


def build_preview_api_payload(row: Mapping[str, Any], *, bucket: str) -> dict[str, Any]:
    """Map one persisted preview manifest into the public API contract."""
    page_count_value = row.get("source_page_count")
    page_count = int(page_count_value) if page_count_value is not None else None
    manifest = _manifest_mapping(row.get("manifest") or row.get("manifest_json"))
    previews: list[dict[str, Any]] = []
    for role in PREVIEW_ROLE_ORDER:
        page_data = manifest.get(role)
        if not isinstance(page_data, Mapping):
            continue
        variants_data = page_data.get("variants")
        variants: dict[str, Any] = {}
        for variant in PREVIEW_VARIANTS:
            raw_variant = variants_data.get(variant) if isinstance(variants_data, Mapping) else None
            if not isinstance(raw_variant, Mapping):
                continue
            key = str(raw_variant.get("key") or "").strip()
            if not key:
                continue
            variants[variant] = {
                **dict(raw_variant),
                "key": key,
                "url": _public_object_url(bucket, key),
            }
        if variants:
            previews.append(
                {
                    "role": role,
                    "page_number": int(page_data.get("page_number") or 0),
                    "variants": variants,
                }
            )

    return {
        "md5": str(row.get("md5") or ""),
        "status": str(row.get("status") or "pending"),
        "recipe_version": str(row.get("recipe_version") or PREVIEW_RECIPE_VERSION),
        "source_page_count": page_count,
        "expected_preview_count": len(select_preview_pages(page_count)) if page_count else None,
        "preview_count": len(previews),
        "previews": previews,
        "error": str(row.get("error_text") or "").strip() or None,
    }


__all__ = [
    "PREVIEW_OBJECT_PREFIX",
    "PREVIEW_RECIPE_VERSION",
    "PREVIEW_ROLE_ORDER",
    "PREVIEW_VARIANTS",
    "PreviewPage",
    "build_preview_api_payload",
    "derive_preview_status",
    "preview_object_key",
    "select_preview_pages",
]
