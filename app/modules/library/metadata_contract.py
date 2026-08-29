"""Versioned schema.org quality rules for Library metadata."""

from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from typing import Any, Mapping
from urllib.parse import urlparse


CONTRACT_VERSION = "schema-org.v2"
SCHEMA_CONTEXT = "https://schema.org"
SUPPORTED_TYPES = frozenset(
    {
        "Article",
        "Book",
        "CreativeWork",
        "HowTo",
        "Legislation",
        "NewsArticle",
        "Newspaper",
        "PublicationIssue",
        "Report",
        "ScholarlyArticle",
        "Thesis",
    }
)
ACCESS_MODES = frozenset({"auditory", "tactile", "textual", "visual"})

_ENGLISH_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 &'()/:,+.\-]*$")
_DATE_RE = re.compile(r"^\d{4}(?:-\d{2})?(?:-\d{2})?$")
_BCP47_RE = re.compile(
    r"^[A-Za-z]{2,8}(?:-[A-Za-z]{4})?(?:-[A-Za-z]{2}|-\d{3})?"
    r"(?:-[A-Za-z0-9]{5,8}|-\d[A-Za-z0-9]{3})*(?:-x(?:-[A-Za-z0-9]{1,8})+)?$"
)
_STANDARD_ROLE_PROPERTIES = {
    "author": "author",
    "editor": "editor",
    "illustrator": "illustrator",
    "translator": "translator",
}
_ALLOWED_FIELDS = {
    "@context",
    "@type",
    "name",
    "author",
    "contributor",
    "editor",
    "translator",
    "illustrator",
    "publisher",
    "datePublished",
    "isbn",
    "inLanguage",
    "description",
    "numberOfPages",
    "bookEdition",
    "about",
    "genre",
    "audience",
    "accessMode",
    "accessModeSufficient",
    "isBasedOn",
}
_BOOK_ONLY_FIELDS = {"isbn", "numberOfPages", "bookEdition", "illustrator"}
_YANALIF_ALPHABET = frozenset(
    "AaBʙCcÇçDdEeƏəFfGgƢƣHhIiJjKkLlMmNnꞐꞑOoƟɵPpQqRrSsŞşTtUuVvXxYyZzƵƶЬь"
)
_ZAMANALIF_ALPHABET = frozenset(
    "AaÄäBbCcÇçDdEeFfGgĞğHhIıİiJjKkLlMmNnÑñOoÖöPpQqRrSsŞşTtUuÜüVvWwXxYyZz"
)
_MIN_SCRIPT_EVIDENCE_LETTERS = 4
_COMPETING_SCRIPT_DOMINANCE = 2


def _issue(code: str, path: str, message: str) -> dict[str, str]:
    return {"code": code, "path": path, "message": message}


def is_english_facet(value: Any) -> bool:
    """Return whether one canonical discovery label is safely English-shaped."""
    if not isinstance(value, str):
        return False
    normalized = " ".join(value.replace("’", "'").split())
    return bool(normalized and _ENGLISH_LABEL_RE.fullmatch(normalized))


def _is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _as_items(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _validate_entity(
    value: Any,
    *,
    path: str,
    allow_role: bool,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if not isinstance(value, Mapping):
        return [_issue("entity_shape", path, "entity must be an object")]
    entity_type = value.get("@type")
    if entity_type in {"Person", "Organization"}:
        if not isinstance(value.get("name"), str) or not value["name"].strip():
            issues.append(
                _issue("entity_name", f"{path}.name", "entity name is required")
            )
        if "role" in value:
            issues.append(
                _issue(
                    "contributor_role_shape",
                    f"{path}.role",
                    "role must use a schema.org Role relationship",
                )
            )
            if not is_english_facet(value.get("role")):
                issues.append(
                    _issue(
                        "role_not_english",
                        f"{path}.role",
                        "canonical role name must be English",
                    )
                )
        allowed = {"@type", "name"}
    elif allow_role and entity_type == "Role":
        role_name = value.get("roleName")
        if not is_english_facet(role_name):
            issues.append(
                _issue(
                    "role_not_english",
                    f"{path}.roleName",
                    "canonical roleName must be English",
                )
            )
        nested = value.get("contributor")
        issues.extend(
            _validate_entity(nested, path=f"{path}.contributor", allow_role=False)
        )
        allowed = {"@type", "roleName", "contributor"}
    else:
        return [_issue("entity_type", f"{path}.@type", "unsupported entity type")]
    for key in value:
        if key not in allowed:
            issues.append(
                _issue(
                    "entity_extra_property",
                    f"{path}.{key}",
                    "unsupported entity property",
                )
            )
    return issues


def _unicode_script(character: str) -> str | None:
    if not character.isalpha():
        return None
    name = unicodedata.name(character, "")
    if "LATIN" in name:
        return "latin"
    if "CYRILLIC" in name:
        return "cyrillic"
    if "ARABIC" in name:
        return "arabic"
    return None


def _description_matches_language(description: str, in_language: str) -> bool:
    """Reject only descriptions with clear competing-script dominance."""
    primary = str(in_language or "").split(",", 1)[0].strip()
    if not primary:
        return True
    lowered = primary.casefold()
    if lowered == "tt-latn-x-yanalif":
        expected_script = "latin"
        expected_compatibility_letters = _YANALIF_ALPHABET
        competing_scripts = {"cyrillic", "arabic"}
    elif lowered in {"tt-latn-x-zaman-alif", "tt-latn-x-zamanalif"}:
        expected_script = "latin"
        expected_compatibility_letters = _ZAMANALIF_ALPHABET
        competing_scripts = {"cyrillic", "arabic"}
    elif lowered.startswith("tt-latn") or lowered == "en" or lowered.startswith(
        "en-"
    ):
        expected_script = "latin"
        expected_compatibility_letters = frozenset()
        competing_scripts = {"cyrillic", "arabic"}
    elif "-cyrl" in lowered:
        expected_script = "cyrillic"
        expected_compatibility_letters = frozenset()
        competing_scripts = {"latin", "arabic"}
    elif "-arab" in lowered:
        expected_script = "arabic"
        expected_compatibility_letters = frozenset()
        competing_scripts = {"latin", "cyrillic"}
    else:
        return True

    expected = 0
    competing = 0
    for character in unicodedata.normalize("NFC", description):
        script = _unicode_script(character)
        if character in expected_compatibility_letters or script == expected_script:
            expected += 1
        elif script in competing_scripts:
            competing += 1
    if expected + competing < _MIN_SCRIPT_EVIDENCE_LETTERS:
        return True
    return not (
        competing > 0 and competing >= _COMPETING_SCRIPT_DOMINANCE * expected
    )


def _validate_defined_term(value: Any, *, path: str) -> list[dict[str, str]]:
    if not isinstance(value, Mapping) or value.get("@type") != "DefinedTerm":
        return [
            _issue(
                "defined_term_shape", path, "about entries must be DefinedTerm objects"
            )
        ]
    issues: list[dict[str, str]] = []
    if not any(
        isinstance(value.get(key), str) and value[key].strip()
        for key in ("name", "termCode")
    ):
        issues.append(
            _issue("defined_term_value", path, "DefinedTerm needs name or termCode")
        )
    termset = value.get("inDefinedTermSet")
    valid_termset = isinstance(termset, str) and _is_url(termset)
    valid_termset = valid_termset or (
        isinstance(termset, Mapping)
        and termset.get("@type") == "DefinedTermSet"
        and isinstance(termset.get("name"), str)
        and bool(termset["name"].strip())
        and set(termset) <= {"@type", "name", "url"}
        and (
            "url" not in termset
            or (isinstance(termset["url"], str) and _is_url(termset["url"]))
        )
    )
    if not valid_termset:
        issues.append(
            _issue(
                "defined_term_set_shape",
                f"{path}.inDefinedTermSet",
                "term set must be a URL or DefinedTermSet object",
            )
        )
    if set(value) - {"@type", "name", "termCode", "inDefinedTermSet"}:
        issues.append(
            _issue(
                "defined_term_extra_property", path, "unsupported DefinedTerm property"
            )
        )
    return issues


def metadata_contract_issues(schema_org: Any) -> list[dict[str, str]]:
    """Return stable issue records for one schema.org payload."""
    if not isinstance(schema_org, Mapping):
        return [_issue("metadata_shape", "$", "metadata must be an object")]
    issues: list[dict[str, str]] = []
    for key in schema_org:
        if key not in _ALLOWED_FIELDS:
            issues.append(_issue("extra_property", f"$.{key}", "unsupported property"))
    if schema_org.get("@context") != SCHEMA_CONTEXT:
        issues.append(
            _issue("context", "$.@context", f"context must be {SCHEMA_CONTEXT}")
        )
    work_type = schema_org.get("@type")
    if work_type not in SUPPORTED_TYPES:
        issues.append(_issue("work_type", "$.@type", "unsupported CreativeWork type"))
    if work_type != "Book":
        for field in sorted(_BOOK_ONLY_FIELDS):
            if field in schema_org:
                issues.append(
                    _issue(
                        "incompatible_property",
                        f"$.{field}",
                        f"{field} is only supported for Book metadata",
                    )
                )
    name = schema_org.get("name")
    if not isinstance(name, str) or not name.strip():
        issues.append(_issue("name", "$.name", "name is required"))

    in_language = schema_org.get("inLanguage")
    if in_language is not None and (
        not isinstance(in_language, str)
        or any(not _BCP47_RE.fullmatch(part.strip()) for part in in_language.split(","))
    ):
        issues.append(
            _issue("language_tag", "$.inLanguage", "invalid BCP 47 language tag")
        )
    description = schema_org.get("description")
    if description is not None:
        if not isinstance(description, str) or not description.strip():
            issues.append(
                _issue("description_shape", "$.description", "description must be text")
            )
        elif isinstance(in_language, str) and not _description_matches_language(
            description, in_language
        ):
            issues.append(
                _issue(
                    "description_script_mismatch",
                    "$.description",
                    "description script does not match inLanguage",
                )
            )

    genre = schema_org.get("genre")
    if genre is not None:
        if (
            not isinstance(genre, list)
            or not genre
            or not all(isinstance(item, str) and item.strip() for item in genre)
        ):
            issues.append(
                _issue("genre_shape", "$.genre", "genre must be a non-empty text array")
            )
        elif any(not is_english_facet(item) for item in genre):
            issues.append(
                _issue("genre_not_english", "$.genre", "genre values must be English")
            )

    audience = schema_org.get("audience")
    if audience is not None:
        audience_items = _as_items(audience)
        valid_shape = isinstance(audience, (Mapping, list)) and bool(audience_items)
        for index, item in enumerate(audience_items):
            if not isinstance(item, Mapping) or item.get("@type") not in {
                "Audience",
                "EducationalAudience",
                "PeopleAudience",
            }:
                valid_shape = False
                continue
            audience_type = item.get("audienceType")
            has_age = any(
                item.get(field) is not None
                for field in ("suggestedMinAge", "suggestedMaxAge")
            )
            if audience_type is None:
                if item.get("@type") != "PeopleAudience" or not has_age:
                    valid_shape = False
            elif not is_english_facet(audience_type):
                issues.append(
                    _issue(
                        "audience_not_english",
                        f"$.audience[{index}].audienceType",
                        "audienceType must be English",
                    )
                )
            allowed = {"@type", "audienceType", "suggestedMinAge", "suggestedMaxAge"}
            if set(item) - allowed:
                issues.append(
                    _issue(
                        "audience_extra_property",
                        f"$.audience[{index}]",
                        "unsupported Audience property",
                    )
                )
            for age_field in ("suggestedMinAge", "suggestedMaxAge"):
                age = item.get(age_field)
                if age is not None and (
                    isinstance(age, bool) or not isinstance(age, int)
                ):
                    issues.append(
                        _issue(
                            "integer_shape",
                            f"$.audience[{index}].{age_field}",
                            f"{age_field} must be integral",
                        )
                    )
        if not valid_shape:
            issues.append(
                _issue(
                    "audience_shape", "$.audience", "audience must use Audience objects"
                )
            )

    for field in ("author", "editor", "illustrator", "translator"):
        for index, item in enumerate(_as_items(schema_org.get(field))):
            issues.extend(
                _validate_entity(item, path=f"$.{field}[{index}]", allow_role=False)
            )
    for index, item in enumerate(_as_items(schema_org.get("contributor"))):
        issues.extend(
            _validate_entity(item, path=f"$.contributor[{index}]", allow_role=True)
        )
    publisher = schema_org.get("publisher")
    if publisher is not None:
        issues.extend(_validate_entity(publisher, path="$.publisher", allow_role=False))

    date_published = schema_org.get("datePublished")
    if date_published is not None and (
        not isinstance(date_published, str) or not _DATE_RE.fullmatch(date_published)
    ):
        issues.append(
            _issue("date_published", "$.datePublished", "invalid publication date")
        )
    for field in ("numberOfPages",):
        value = schema_org.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            issues.append(
                _issue("integer_shape", f"$.{field}", f"{field} must be integral")
            )
    if "bookEdition" in schema_org and not isinstance(
        schema_org.get("bookEdition"), str
    ):
        issues.append(
            _issue("book_edition_shape", "$.bookEdition", "bookEdition must be text")
        )
    if "suggestedMinAge" in schema_org:
        issues.append(
            _issue(
                "suggested_age_location",
                "$.suggestedMinAge",
                "age belongs inside PeopleAudience",
            )
        )

    isbn = schema_org.get("isbn")
    if isbn is not None and (
        not isinstance(isbn, list)
        or not isbn
        or any(not isinstance(item, str) or not item.strip() for item in isbn)
    ):
        issues.append(
            _issue("isbn_shape", "$.isbn", "isbn must be a non-empty text array")
        )

    access_mode = schema_org.get("accessMode")
    if access_mode is not None:
        modes = _as_items(access_mode)
        if not modes or any(item not in ACCESS_MODES for item in modes):
            issues.append(
                _issue(
                    "access_mode_value", "$.accessMode", "unsupported accessMode value"
                )
            )
    sufficient = schema_org.get("accessModeSufficient")
    if sufficient is not None:
        lists = _as_items(sufficient)
        valid = bool(lists)
        for item in lists:
            if not isinstance(item, Mapping) or item.get("@type") != "ItemList":
                valid = False
                continue
            elements = item.get("itemListElement")
            if (
                not isinstance(elements, list)
                or not elements
                or any(mode not in ACCESS_MODES for mode in elements)
            ):
                valid = False
        if not valid:
            issues.append(
                _issue(
                    "access_mode_sufficient_shape",
                    "$.accessModeSufficient",
                    "accessModeSufficient must contain ItemList objects",
                )
            )

    for index, item in enumerate(_as_items(schema_org.get("about"))):
        issues.extend(_validate_defined_term(item, path=f"$.about[{index}]"))
    based_on = schema_org.get("isBasedOn")
    if based_on is not None:
        if not isinstance(based_on, Mapping) or set(based_on) - {
            "@type",
            "name",
            "author",
            "inLanguage",
            "url",
        }:
            issues.append(
                _issue(
                    "based_on_shape",
                    "$.isBasedOn",
                    "isBasedOn must be a CreativeWork object",
                )
            )
        elif "url" in based_on and (
            not isinstance(based_on["url"], list)
            or any(
                not isinstance(url, str) or not _is_url(url) for url in based_on["url"]
            )
        ):
            issues.append(
                _issue(
                    "url_shape", "$.isBasedOn.url", "URLs must be absolute HTTP(S) URLs"
                )
            )
    return issues


def reshape_english_contributor_roles(
    schema_org: Mapping[str, Any],
) -> tuple[dict[str, Any], bool, bool]:
    """Convert legacy English contributor roles without semantic regeneration."""
    updated = deepcopy(dict(schema_org))
    raw = updated.get("contributor")
    if raw is None:
        return updated, False, False
    contributors = raw if isinstance(raw, list) else [raw]
    retained: list[Any] = []
    promoted: dict[str, list[dict[str, Any]]] = {}
    changed = False
    for item in contributors:
        if not isinstance(item, Mapping) or "role" not in item:
            retained.append(item)
            continue
        role = str(item.get("role") or "").strip()
        if not is_english_facet(role):
            return updated, False, True
        entity = {key: deepcopy(value) for key, value in item.items() if key != "role"}
        property_name = _STANDARD_ROLE_PROPERTIES.get(role.casefold())
        if property_name:
            promoted.setdefault(property_name, []).append(entity)
        else:
            retained.append(
                {
                    "@type": "Role",
                    "roleName": role,
                    "contributor": entity,
                }
            )
        changed = True
    if retained:
        updated["contributor"] = retained
    else:
        updated.pop("contributor", None)
    for property_name, entities in promoted.items():
        existing = _as_items(updated.get(property_name))
        updated[property_name] = existing + entities
    return updated, changed, False


__all__ = [
    "ACCESS_MODES",
    "CONTRACT_VERSION",
    "SCHEMA_CONTEXT",
    "SUPPORTED_TYPES",
    "is_english_facet",
    "metadata_contract_issues",
    "reshape_english_contributor_roles",
]
