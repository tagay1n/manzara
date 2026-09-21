"""Versioned Gemini prompt for one raw bibliographic person name."""

from __future__ import annotations


PERSONALITY_NORMALIZATION_PROMPT_VERSION = "personality-components-v1"


def build_personality_normalization_prompt(raw_name: str) -> str:
    """Return the fixed, single-person prompt without placing identity logic in Gemini."""
    return f"""You normalize exactly one bibliographic person's raw name into JSON.
Prompt version: {PERSONALITY_NORMALIZATION_PROMPT_VERSION}.

Names may be Tatar, Russian, and other cultures and scripts. Return only the
declared JSON object, with no prose and no unknown fields. Every string is
trimmed; use null for unknown or absent values. Never invent an expansion of an
initial. Preserve a supplied initial when a full form cannot be established.

Fields: surname_full, surname_initials, name_full, name_initials,
father_name_full, father_name_initials, title, sex. The sex field is only M, F,
or null. Initials are normalized as А. (or several initials separated by one
space). father_name_full is the father's base given name, not an inflected
patronymic. title is an honorific/rank, not identity. Provide at least one
surname or personal-name component; the application derives display text.

Examples (input => selected interpretation):
- "Тукай Габдулла Мөхәммәтгариф улы" => surname_full="Тукай",
  name_full="Габдулла", father_name_full="Мөхәммәтгариф", sex="M".
- "Әхмәтова Ләйсән Рифкать кызы" => surname_full="Әхмәтова",
  name_full="Ләйсән", father_name_full="Рифкать", sex="F".
- "Пушкин А. С." => surname_full="Пушкин", name_initials="А.",
  father_name_initials="С.", sex=null; unknown full components stay null.
- "Толстой Лев Николаевич" => surname_full="Толстой", name_full="Лев",
  father_name_full="Николай", sex="M"; Николаевич is not father_name_full.
- "хәзрәт Галимҗан" => title="хәзрәт", name_full="Галимҗан", sex=null.
- "Shakespeare William" => surname_full="Shakespeare", name_full="William",
  father_name_full=null, sex=null.
- "Т. Г." => surname_initials="Т.", name_initials="Г."; do not guess words.
- A malformed input such as "---" must not be fabricated into a person. Local
  validation rejects unusable output, so return no invented components.

Raw name to normalize (one person only): {raw_name}
"""


__all__ = ["PERSONALITY_NORMALIZATION_PROMPT_VERSION", "build_personality_normalization_prompt"]
