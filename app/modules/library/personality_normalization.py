"""Pure contracts for Library personality normalization.

Persistence and Gemini orchestration deliberately live in dedicated owners; this
module keeps strict response contracts and deterministic identity formatting
independent of PostgreSQL discovery.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    letter = value[:-1] if value.endswith(".") else value
    if 1 <= len(letter) <= 2 and letter.isalpha():
        return f"{letter[0].upper()}{letter[1:].lower()}."
    raise ValueError("initial fields require one or two alphabetic characters")


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
    reason: str | None = Field(max_length=300)
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
    "personality_identity_key",
    "personality_source_fingerprint",
    "preprocess_personality_name_for_model",
    "storage_components",
]
