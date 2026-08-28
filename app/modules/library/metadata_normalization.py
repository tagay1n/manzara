"""Schema.org normalization preserved from monocorpus metadata extraction."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from app.modules.library.runtime.metadata.isbn_utils import canonicalize_isbn_values
from app.modules.library.runtime.metadata.url_utils import normalize_url_list

UNKNOWN_VALUES = {"", "unknown", "неизвестно", "none", "null", "n/a"}
WHITESPACE_RE = re.compile(r"\s+")

def _normalize_base_schema_org(schema_org: dict) -> dict:
    """Normalize base metadata fields before storing in `metadata.schema_org`."""
    updated = dict(schema_org)

    _set_or_drop(updated, "name", _clean_text(updated.get("name"), max_len=600))
    _set_or_drop(updated, "description", _clean_text(updated.get("description"), max_len=5000))
    _set_or_drop(updated, "audience", _normalize_audience(updated.get("audience")))
    _set_or_drop(updated, "inLanguage", _normalize_in_language(updated.get("inLanguage")))
    _set_or_drop(updated, "datePublished", _normalize_date_published(updated.get("datePublished")))
    _set_or_drop(updated, "numberOfPages", _normalize_int(updated.get("numberOfPages"), 1, 20_000))
    _set_or_drop(updated, "bookEdition", _clean_text(updated.get("bookEdition"), max_len=120))
    _set_or_drop(updated, "genre", _normalize_string_list(updated.get("genre"), max_len=120, lower_case=True))
    _set_or_drop(updated, "author", _normalize_people(updated.get("author"), keep_role=False))
    _set_or_drop(updated, "contributor", _normalize_contributors(updated.get("contributor")))
    for field in ("editor", "translator", "illustrator"):
        _set_or_drop(updated, field, _normalize_people(updated.get(field), keep_role=False))
    _set_or_drop(updated, "publisher", _normalize_publisher(updated.get("publisher")))
    _set_or_drop(updated, "isBasedOn", _normalize_is_based_on(updated.get("isBasedOn")))

    normalized_isbn = canonicalize_isbn_values(updated.get("isbn"))
    if normalized_isbn:
        updated["isbn"] = normalized_isbn
    else:
        updated.pop("isbn", None)

    about_items = updated.get("about")
    about = about_items if isinstance(about_items, list) else ([about_items] if about_items else [])

    seen = set()
    normalized_about = []
    for item in about:
        if not isinstance(item, dict):
            continue
        termset = _normalize_termset(item.get("inDefinedTermSet"))
        term_code = _clean_text(item.get("termCode") or item.get("name"), max_len=500)
        if not termset or not term_code:
            continue
        termset_name = _termset_name(termset)
        if termset_name.casefold() in {"ddc", "genre", "categorypath"}:
            continue
        key = (termset_name.casefold(), term_code.casefold())
        if key in seen:
            continue
        seen.add(key)
        normalized_about.append(
            {
                "@type": "DefinedTerm",
                "termCode": term_code,
                "inDefinedTermSet": termset,
            }
        )

    if normalized_about:
        updated["about"] = normalized_about
    elif "about" in updated:
        updated.pop("about", None)

    updated.pop("additionalProperty", None)
    return updated

def _set_or_drop(data: dict, key: str, value):
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value


def _clean_text(value, max_len: int = 1000):
    if value is None:
        return None
    text = WHITESPACE_RE.sub(" ", str(value)).strip()
    if not text:
        return None
    if text.casefold() in UNKNOWN_VALUES:
        return None
    return text[:max_len]


def _normalize_string_list(value, max_len: int = 200, lower_case: bool = False):
    items = value if isinstance(value, list) else [value]
    out = []
    seen = set()
    for item in items:
        text = _clean_text(item, max_len=max_len)
        if not text:
            continue
        if lower_case:
            text = text.lower()
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out or None


def _normalize_people(value, keep_role: bool):
    items = value if isinstance(value, list) else [value]
    out = []
    seen = set()
    for item in items:
        if isinstance(item, dict):
            name = _clean_text(item.get("name"), max_len=300)
            person_type = _clean_text(item.get("@type"), max_len=40) or "Person"
            role = _clean_text(item.get("role"), max_len=120) if keep_role else None
        else:
            name = _clean_text(item, max_len=300)
            person_type = "Person"
            role = None
        if not name:
            continue
        key = (name.casefold(), person_type.casefold(), (role or "").casefold())
        if key in seen:
            continue
        seen.add(key)
        normalized = {"@type": person_type, "name": name}
        if role:
            normalized["role"] = role
        out.append(normalized)
    return out or None


def _normalize_contributors(value):
    items = value if isinstance(value, list) else [value]
    out = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            people = _normalize_people(item, keep_role=False) or []
            out.extend(people)
            continue
        if str(item.get("@type") or "") == "Role":
            role_name = _clean_text(item.get("roleName"), max_len=120)
            nested = _normalize_people(item.get("contributor"), keep_role=False)
            if not role_name or not nested:
                continue
            normalized = {
                "@type": "Role",
                "roleName": role_name,
                "contributor": nested[0],
            }
            key = ("role", role_name.casefold(), nested[0]["name"].casefold())
        else:
            people = _normalize_people(item, keep_role=False)
            if not people:
                continue
            normalized = people[0]
            key = ("entity", normalized["@type"].casefold(), normalized["name"].casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return out or None


def _normalize_audience(value):
    items = value if isinstance(value, list) else [value]
    normalized = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        audience_type = _clean_text(item.get("audienceType"), max_len=400)
        item_type = _clean_text(item.get("@type"), max_len=40)
        if item_type not in {"Audience", "EducationalAudience", "PeopleAudience"} or not audience_type:
            continue
        result = {"@type": item_type, "audienceType": audience_type}
        if item_type == "PeopleAudience":
            for key in ("suggestedMinAge", "suggestedMaxAge"):
                number = _normalize_int(item.get(key), 0, 150)
                if number is not None:
                    result[key] = number
        dedupe = (item_type.casefold(), audience_type.casefold())
        if dedupe in seen:
            continue
        seen.add(dedupe)
        normalized.append(result)
    if not normalized:
        return None
    return normalized[0] if len(normalized) == 1 else normalized


def _normalize_termset(value):
    if isinstance(value, dict):
        name = _clean_text(value.get("name"), max_len=120)
        if str(value.get("@type") or "") != "DefinedTermSet" or not name:
            return None
        result = {"@type": "DefinedTermSet", "name": name}
        url = _clean_text(value.get("url"), max_len=500)
        if url:
            result["url"] = url
        return result
    name = _clean_text(value, max_len=120)
    if not name:
        return None
    parsed = urlparse(name)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return name
    return {"@type": "DefinedTermSet", "name": name}


def _termset_name(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return str(value or "")


def _normalize_publisher(value):
    if isinstance(value, dict):
        name = _clean_text(value.get("name"), max_len=400)
    else:
        name = _clean_text(value, max_len=400)
    if not name:
        return None
    return {"@type": "Organization", "name": name}


def _normalize_is_based_on(value):
    if not isinstance(value, dict):
        return None

    normalized = {}
    work_type = _clean_text(value.get("@type"), max_len=80) or "CreativeWork"
    normalized["@type"] = work_type

    if name := _clean_text(value.get("name"), max_len=600):
        normalized["name"] = name
    if author := _normalize_people(value.get("author"), keep_role=False):
        normalized["author"] = author
    if in_language := _normalize_in_language(value.get("inLanguage")):
        normalized["inLanguage"] = in_language
    if urls := normalize_url_list(value.get("url")):
        normalized["url"] = urls

    if len(normalized) == 1 and normalized.get("@type") == "CreativeWork":
        return None
    return normalized


def _normalize_date_published(value):
    raw = _clean_text(value, max_len=40)
    if not raw:
        return None
    raw = raw.replace("/", "-")
    if re.fullmatch(r"\d{4}(-\d{2})?(-\d{2})?", raw):
        year = int(raw[:4])
        return raw if 1500 <= year <= 2100 else None
    return None


def _normalize_int(value, min_value: int, max_value: int):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if min_value <= value <= max_value else None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        int_val = int(value)
        return int_val if min_value <= int_val <= max_value else None
    text = _clean_text(value, max_len=40)
    if not text:
        return None
    match = re.search(r"\d+", text)
    if not match:
        return None
    int_val = int(match.group(0))
    return int_val if min_value <= int_val <= max_value else None


def _normalize_in_language(value):
    text = _clean_text(value, max_len=200)
    if not text:
        return None
    codes = [part.strip() for part in text.split(",")]
    codes = [code for code in codes if code]
    if not codes:
        return None
    normalized = []
    seen = set()
    for code in codes:
        key = code.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(code)
    return ", ".join(sorted(normalized))

normalize_base_schema_org = _normalize_base_schema_org

__all__ = ["normalize_base_schema_org"]
