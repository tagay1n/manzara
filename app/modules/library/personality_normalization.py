"""Pure contracts for Library personality normalization.

Persistence and Gemini orchestration deliberately live in dedicated owners; this
module keeps extraction and deterministic identity formatting independently
testable.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator


_PERSON_ROLES = ("author", "editor", "translator", "illustrator", "contributor")
_COMPONENT_FIELDS = (
    "surname_full",
    "surname_initial",
    "name_full",
    "name_initial",
    "father_name_full",
    "father_name_initial",
    "title",
)
_CYRILLIC_NAME_WORD = re.compile(
    r"[А-Яа-яЁёӘәҖҗҢңӨөҮүҺһІіҒғҚқҪҫ]{2,}"
)


def _text(value: Any) -> str | None:
    """Return compact NFC text, treating empty values as absent."""
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFC", " ".join(value.split()))
    return normalized or None


def _initials(value: str | None) -> str | None:
    if value is None:
        return None
    # Explicit transliteration units discussed for this catalog: х, ш, ц, ю.
    # They represent one initial, rather than an arbitrary multi-letter word.
    transliterations = {"kh": "Kh", "sh": "Sh", "ts": "Ts", "ju": "Ju"}
    letter = value[:-1] if value.endswith(".") else value
    if len(letter) == 1 and letter.isalpha():
        return f"{letter.upper()}."
    if letter.casefold() in transliterations:
        return f"{transliterations[letter.casefold()]}."
    raise ValueError("initial fields require one letter or Kh., Sh., Ts., Ju.")


class PersonComponents(BaseModel):
    """Strict Gemini response contract; canonical text is derived locally."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    surname_full: str | None = None
    surname_initial: str | None = None
    name_full: str | None = None
    name_initial: str | None = None
    father_name_full: str | None = None
    father_name_initial: str | None = None
    title: str | None = None
    sex: Literal["M", "F"] | None = None

    @model_validator(mode="after")
    def normalize_and_validate(self) -> "PersonComponents":
        for field in _COMPONENT_FIELDS:
            value = _text(getattr(self, field))
            if field.endswith("_initial"):
                value = _initials(value)
            setattr(self, field, value)
        if not any(
            getattr(self, field)
            for field in (
                "surname_full",
                "surname_initial",
                "name_full",
                "name_initial",
            )
        ):
            raise ValueError("at least one usable surname or personal-name component is required")
        return self


class PersonalityResponse(BaseModel):
    """Complete model decision; negative outcomes carry no identity components."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    outcome: Literal["normalized", "not_person", "unusable"]
    reason: str | None
    surname_full: str | None
    surname_initial: str | None
    name_full: str | None
    name_initial: str | None
    father_name_full: str | None
    father_name_initial: str | None
    title: str | None
    sex: Literal["M", "F"] | None

    @model_validator(mode="after")
    def validate_decision(self) -> "PersonalityResponse":
        if self.outcome == "normalized":
            if self.reason is not None:
                raise ValueError("normalized outcomes require a null reason")
            components = PersonComponents.model_validate(self.model_dump(exclude={"outcome", "reason"}))
            for field, value in components.model_dump().items():
                setattr(self, field, value)
        else:
            if not _text(self.reason):
                raise ValueError("negative outcomes require a nonblank reason")
            if any(getattr(self, field) is not None for field in (*_COMPONENT_FIELDS, "sex")):
                raise ValueError("negative outcomes must have null identity components")
        return self

    def person_components(self) -> PersonComponents | None:
        if self.outcome != "normalized":
            return None
        return PersonComponents.model_validate(self.model_dump(exclude={"outcome", "reason"}))


@dataclass(frozen=True)
class PersonalityCandidate:
    raw_name: str
    document_count: int
    mention_count: int
    roles: tuple[str, ...]
    document_languages: tuple[str, ...] = ()


def _as_items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _types(value: dict[str, Any]) -> set[str]:
    raw = value.get("@type", value.get("type"))
    return {str(item).strip().casefold() for item in _as_items(raw) if str(item).strip()}


def _person_name(value: Any) -> str | None:
    if not isinstance(value, dict) or "person" not in _types(value):
        return None
    return _text(value.get("name"))


def _relationship_people(value: Any) -> list[str]:
    names: list[str] = []
    for item in _as_items(value):
        direct = _person_name(item)
        if direct:
            names.append(direct)
            continue
        if isinstance(item, dict) and "role" in _types(item):
            for nested in _as_items(item.get("contributor")):
                nested_name = _person_name(nested)
                if nested_name:
                    names.append(nested_name)
    return names


def _document_languages(schema_org: dict[str, Any]) -> tuple[str, ...]:
    """Return safe BCP-47-like language hints from untrusted metadata."""
    values = {
        value
        for item in _as_items(schema_org.get("inLanguage"))
        if (value := _text(item)) and re.fullmatch(r"[A-Za-z0-9-]{1,35}", value)
    }
    return tuple(sorted(values, key=str.casefold))


def extract_personality_candidates(documents: list[dict[str, Any]]) -> list[PersonalityCandidate]:
    """Aggregate exact raw Person names from the supported JSON-LD relations."""
    aggregate: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"documents": set(), "mentions": 0, "roles": set(), "languages": set()}
    )
    for document in documents:
        schema_org = document.get("schema_org")
        if isinstance(schema_org, str):
            try:
                schema_org = json.loads(schema_org)
            except json.JSONDecodeError:
                schema_org = None
        if not isinstance(schema_org, dict):
            continue
        document_id = str(document.get("md5") or "")
        document_languages = _document_languages(schema_org)
        for role in _PERSON_ROLES:
            for raw_name in _relationship_people(schema_org.get(role)):
                item = aggregate[raw_name]
                if document_id:
                    item["documents"].add(document_id)
                item["mentions"] += 1
                item["roles"].add(role)
                item["languages"].update(document_languages)
    return [
        PersonalityCandidate(
            raw_name=raw_name,
            document_count=len(item["documents"]),
            mention_count=int(item["mentions"]),
            roles=tuple(sorted(item["roles"])),
            document_languages=tuple(sorted(item["languages"], key=str.casefold)),
        )
        for raw_name, item in sorted(aggregate.items(), key=lambda pair: pair[0].casefold())
    ]


def preprocess_personality_name_for_model(raw_name: str) -> str:
    """Repair leading OCR-confused Cyrillic З initials for model input only."""
    if not raw_name.startswith("3.") or not _CYRILLIC_NAME_WORD.search(raw_name[2:]):
        return raw_name

    chunks: list[str] = []
    cursor = 0
    while raw_name.startswith("3.", cursor):
        chunks.append("З.")
        cursor += 2
        whitespace_start = cursor
        while cursor < len(raw_name) and raw_name[cursor].isspace():
            cursor += 1
        chunks.append(raw_name[whitespace_start:cursor])
    return "".join(chunks) + raw_name[cursor:]


def build_canonical_name(components: PersonComponents) -> str:
    """Derive the visible canonical name without using titles as identity."""
    parts = [
        components.surname_full or components.surname_initial,
        components.name_full or components.name_initial,
        components.father_name_full or components.father_name_initial,
    ]
    result = [part for part in parts if part]
    if components.father_name_full and components.sex == "M":
        result.append("улы")
    elif components.father_name_full and components.sex == "F":
        result.append("кызы")
    if components.title:
        result.append(components.title)
    return " ".join(result)


def personality_identity_key(components: PersonComponents) -> str:
    """Stable exact-match key; titles and inferred sex are intentionally excluded."""
    values = [
        components.surname_full or components.surname_initial or "",
        components.name_full or components.name_initial or "",
        components.father_name_full or components.father_name_initial or "",
    ]
    return "\x1f".join(unicodedata.normalize("NFC", value).casefold() for value in values)


def personality_source_fingerprint(candidate: PersonalityCandidate) -> str:
    """Version-independent candidate fingerprint for rerun eligibility."""
    payload = [candidate.raw_name, candidate.document_count, candidate.mention_count,
               list(candidate.roles), list(candidate.document_languages)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def storage_components(components: PersonComponents) -> dict[str, str | None]:
    """Map singular external fields onto the existing durable column names."""
    values = components.model_dump()
    return {
        "surname_full": values["surname_full"],
        "surname_initials": values["surname_initial"],
        "name_full": values["name_full"],
        "name_initials": values["name_initial"],
        "father_name_full": values["father_name_full"],
        "father_name_initials": values["father_name_initial"],
        "title": values["title"],
        "sex": values["sex"],
    }


__all__ = [
    "PersonComponents",
    "PersonalityResponse",
    "PersonalityCandidate",
    "build_canonical_name",
    "extract_personality_candidates",
    "personality_identity_key",
    "personality_source_fingerprint",
    "preprocess_personality_name_for_model",
    "storage_components",
]
