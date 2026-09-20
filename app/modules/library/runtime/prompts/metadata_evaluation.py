"""Prompt templates for metadata evaluation and library applicability."""

from __future__ import annotations

import json
from typing import Any


LIBRARY_APPLICABILITY_TASK_TEXT = (
    "You are helping build a high-quality digital library of Tatar-language documents. "
    "For each document, do three tasks in order: "
    "(1) fill missing metadata fields when evidence is present, "
    "(2) decide library applicability, "
    "(3) assign DDC-based library classification when applicable=true. "
    "Decisions are about inclusion in a public library collection for general readers. "
    "Return strict JSON with fields: applicable(bool), reason(str|null), "
    "metadata_patch(object), library_ddc(str|null), library_path(array|null). "
    "Return JSON only."
)

METADATA_GAP_FILL_RULES_TEXT = (
    "Metadata gap filling rules (apply first): "
    "only use verifiable information from provided evidence; if uncertain do not guess; "
    "do not fabricate metadata; keep UTF-8; "
    "when multiple values are explicitly present include all as arrays. "
    "For each requested field, put either extracted/normalized value or null in metadata_patch."
)

LIBRARY_APPLICABILITY_RULES_TEXT = (
    "Decide if the document should be included in a public library collection for general readers. "
    "Use applicable=true for reader-oriented materials: books, textbooks, educational materials, "
    "literature, children works, biographies, history, cultural works, high-quality journalism, "
    "popular science, dictionaries, encyclopedias. "
    "Use applicable=false for government/legal/bureaucratic/utility documents: laws, decrees, "
    "regulations, standards, budgets, procurement docs, forms, schedules, meeting minutes, "
    "administrative paperwork, low-value fragments. "
    "If uncertain, prefer applicable=false. Reason must be short (2-8 words)."
)

LIBRARY_CLASSIFICATION_RULES_TEXT = (
    "library_ddc and library_path must both be null when applicable=false. "
    "When applicable=true, both fields are mandatory: "
    "library_ddc (string, 3 digits with optional decimal extension, e.g. 600 or 621.3) "
    "and library_path (array of 2-8 category labels, top->leaf). "
    "library_path labels must be in English. "
    "Use one of known_classifications if there is a close match; otherwise "
    "suggest a new classification with best-fit ddc and path. "
    "If upstream_metadata is provided, use it only as supporting evidence when it "
    "is consistent with the document. Ignore fields that contradict document content."
)

MISSING_FIELD_REQUESTS = {
    "isbn": "Please add `isbn` (array of ISBN values) or return null.",
    "datePublished": "Please add `datePublished` (YYYY or YYYY-MM-DD) or return null.",
    "numberOfPages": "Please add `numberOfPages` (integer) or return null.",
    "name": "Please add `name` (document title) or return null.",
    "author": "Please add `author` (schema.org Person/Organization list) or return null.",
    "publisher": "Please add `publisher` (schema.org Organization) or return null.",
    "genre": "Please normalize `genre` (array) from evidence, or return null if unknown.",
    "description": "Please add `description` (1-3 concise sentences) or return null.",
}

OUTPUT_CONTRACT_TEXT = (
    "Output contract: return one JSON object with exactly these top-level fields: "
    "applicable, reason, metadata_patch, library_ddc, library_path. "
    "Do not include markdown, code fences, or explanatory text."
)


def _requested_patch_fields(work_type: Any, missing_fields: list[str] | None) -> list[str]:
    fields: list[str] = []
    for field in missing_fields or []:
        if field not in MISSING_FIELD_REQUESTS or field in fields:
            continue
        if field in {"isbn", "numberOfPages"} and work_type != "Book":
            continue
        fields.append(field)
    return fields


def _build_missing_fields_text(missing_fields: list[str]) -> str:
    items = missing_fields
    if not items:
        return "No metadata gaps are requested in this run; metadata_patch must be null."
    lines = ["Missing metadata fields to fill (value or null):"]
    for field in items:
        lines.append(f"- {MISSING_FIELD_REQUESTS[field]}")
    return "\n".join(lines)


def _build_metadata_patch_shape_text(
    work_type: Any, missing_fields: list[str]
) -> str:
    persisted_type = work_type if isinstance(work_type, str) and work_type else "unknown"
    fields = missing_fields
    allowed_keys = ", ".join(fields) if fields else "none"
    text = (
        f"The persisted schema.org work type is `{persisted_type}`. "
        "metadata_patch must be a schema.org-compatible PARTIAL object (or null) for that work type. "
        "You must not change `@type`; it is not a patch key. "
        f"Allowed keys: {allowed_keys}. "
        "Do not include keys that were not requested."
    )
    if "genre" in fields:
        text += " Keep genre values as concise canonical English labels."
    if "author" in fields:
        text += " Use author=[{'@type':'Person'|'Organization','name':...}]."
    if "publisher" in fields:
        text += " Use publisher={'@type':'Organization','name':...}."
    if "description" in fields:
        text += (
            " Keep description in the same language and script as inLanguage; "
            "use English only for an English document."
        )
    if persisted_type == "Book" and {"isbn", "numberOfPages"} & set(fields):
        text += (
            " For Books, isbn and numberOfPages are optional, evidence-based gap fills "
            "only when requested."
        )
    return text


def build_library_applicability_prompt(
    payload: dict[str, Any],
    *,
    content_excerpt: str | None = None,
) -> list[dict[str, str]]:
    """Build a structured prompt: gap-fill metadata, then evaluate, then classify."""
    requested_fields = _requested_patch_fields(
        payload.get("work_type"), payload.get("missing_fields")
    )
    missing_fields_text = _build_missing_fields_text(requested_fields)
    metadata_patch_shape_text = _build_metadata_patch_shape_text(
        payload.get("work_type"), requested_fields
    )
    prompt = [
        {"text": LIBRARY_APPLICABILITY_TASK_TEXT},
        {"text": METADATA_GAP_FILL_RULES_TEXT},
        {"text": metadata_patch_shape_text},
        {"text": missing_fields_text},
        {"text": LIBRARY_APPLICABILITY_RULES_TEXT},
        {"text": LIBRARY_CLASSIFICATION_RULES_TEXT},
        {"text": OUTPUT_CONTRACT_TEXT},
        {
            "text": (
                "Now use known metadata, upstream metadata (if any), and content excerpt or PDF slice "
                "to produce the required JSON response."
            )
        },
        {"text": json.dumps(payload, ensure_ascii=False)},
    ]
    if content_excerpt:
        prompt.append({"text": "CONTENT_EXCERPT:\n" + str(content_excerpt)})
    return prompt


__all__ = [
    "LIBRARY_APPLICABILITY_TASK_TEXT",
    "METADATA_GAP_FILL_RULES_TEXT",
    "LIBRARY_APPLICABILITY_RULES_TEXT",
    "LIBRARY_CLASSIFICATION_RULES_TEXT",
    "OUTPUT_CONTRACT_TEXT",
    "build_library_applicability_prompt",
]
