"""Shared Gemini structured-request transport with uploaded-file cleanup."""

from __future__ import annotations

import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

from google import genai
from google.genai import types
from pydantic import BaseModel


def _state_name(file_state: Any) -> str:
    state = getattr(file_state, "state", None)
    return str(getattr(state, "name", None) or state or "").upper()


def _stream_text(response: Any) -> str:
    parts: list[str] = []
    for chunk in response:
        candidates = getattr(chunk, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                value = getattr(part, "text", None)
                if value:
                    parts.append(str(value))
        if not candidates:
            value = getattr(chunk, "text", None)
            if value:
                parts.append(str(value))
    return "".join(parts)


def _drop_unsupported_schema_keywords(value: Any) -> Any:
    """Remove JSON Schema keywords unsupported by Gemini response_schema."""
    if isinstance(value, dict):
        return {
            key: _drop_unsupported_schema_keywords(item)
            for key, item in value.items()
            if key not in {"additionalProperties", "additional_properties"}
        }
    if isinstance(value, list):
        return [_drop_unsupported_schema_keywords(item) for item in value]
    return value


def _transport_response_schema(response_schema: Any) -> Any:
    """Build a transport-safe schema without weakening local validation."""
    if (
        isinstance(response_schema, type)
        and issubclass(response_schema, BaseModel)
    ):
        raw_schema = response_schema.model_json_schema()
    elif isinstance(response_schema, Mapping):
        raw_schema = deepcopy(dict(response_schema))
    else:
        return response_schema
    return _drop_unsupported_schema_keywords(raw_schema)


def generate_structured_json(
    *,
    api_key: str,
    model_name: str,
    contents: Sequence[Any],
    response_schema: Any,
    files: Mapping[Path, str] | None = None,
    timeout_seconds: int = 360,
) -> str:
    """Run one structured request and always delete uploaded Gemini files."""
    client = genai.Client(api_key=api_key)
    uploaded: list[Any] = []
    try:
        for path, mime_type in (files or {}).items():
            item = client.files.upload(
                file=str(path),
                config={"mime_type": str(mime_type)},
            )
            uploaded.append(item)
            deadline = time.monotonic() + 60
            while _state_name(item) != "ACTIVE":
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Gemini file did not become active: {path}")
                time.sleep(0.3)
                item = client.files.get(name=item.name)
                uploaded[-1] = item

        response = client.models.generate_content_stream(
            model=model_name,
            contents=[*contents, *uploaded],
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
                response_schema=_transport_response_schema(response_schema),
                candidate_count=1,
                seed=1552,
                http_options=types.HttpOptions(
                    timeout=max(1, int(timeout_seconds)) * 1000
                ),
            ),
        )
        return _stream_text(response)
    finally:
        for item in uploaded:
            try:
                client.files.delete(name=item.name)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"gemini request: uploaded file cleanup failed "
                    f"name={getattr(item, 'name', '')} error={type(exc).__name__}: {exc}",
                    flush=True,
                )


__all__ = ["generate_structured_json"]
