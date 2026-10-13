"""Catalog language-tag spelling shared by metadata consumers."""

ZAMANALIF_LANGUAGE_TAG = "tt-Latn-x-zaman"
LEGACY_ZAMANALIF_LANGUAGE_TAGS = frozenset(
    {"tt-latn-x-zamanalif", "tt-latn-x-zaman-alif"}
)


def canonical_language_tag(tag: str) -> str:
    """Normalize agreed Zamanalif spellings; preserve other language tags."""
    cleaned = tag.strip()
    if cleaned.casefold() in LEGACY_ZAMANALIF_LANGUAGE_TAGS | {
        ZAMANALIF_LANGUAGE_TAG.casefold()
    }:
        return ZAMANALIF_LANGUAGE_TAG
    return cleaned
