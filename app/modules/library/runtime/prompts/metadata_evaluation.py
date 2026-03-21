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
    "do not fabricate author/date/ISBN/page count; keep UTF-8; "
    "when multiple values are explicitly present include all as arrays. "
    "For each requested field, put either extracted/normalized value or null in metadata_patch. "
    "If page count is missing then return null."
)

METADATA_PATCH_SHAPE_TEXT = (
    "metadata_patch must be a schema.org Book-compatible PARTIAL object (or null). "
    "Allowed keys: name, author, publisher, datePublished, isbn, inLanguage, description, "
    "numberOfPages, genre. "
    "Do not include keys that were not requested. "
    "Use schema.org-compatible nested shapes: "
    "author=[{'@type':'Person'|'Organization','name':...}], "
    "publisher={'@type':'Organization','name':...}."
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
    "If upstream_metadata is provided, treat it as trustworthy external metadata "
    "and use it together with document content."
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


def _build_missing_fields_text(missing_fields: list[str] | None) -> str:
    items = [field for field in (missing_fields or []) if field in MISSING_FIELD_REQUESTS]
    if not items:
        return "No metadata gaps are requested in this run; metadata_patch must be null."
    lines = ["Missing metadata fields to fill (value or null):"]
    for field in items:
        lines.append(f"- {MISSING_FIELD_REQUESTS[field]}")
    return "\n".join(lines)


def build_library_applicability_prompt(
    payload: dict[str, Any],
    *,
    content_excerpt: str | None = None,
) -> list[dict[str, str]]:
    """Build a structured prompt: gap-fill metadata, then evaluate, then classify."""
    missing_fields_text = _build_missing_fields_text(payload.get("missing_fields"))
    prompt = [
        {"text": LIBRARY_APPLICABILITY_TASK_TEXT},
        {"text": METADATA_GAP_FILL_RULES_TEXT},
        {"text": METADATA_PATCH_SHAPE_TEXT},
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
    "METADATA_PATCH_SHAPE_TEXT",
    "LIBRARY_APPLICABILITY_RULES_TEXT",
    "LIBRARY_CLASSIFICATION_RULES_TEXT",
    "OUTPUT_CONTRACT_TEXT",
    "build_library_applicability_prompt",
]
