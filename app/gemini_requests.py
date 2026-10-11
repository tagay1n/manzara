"""Shared Gemini structured-request transport with uploaded-file cleanup."""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.gemini_runtime import record_gemini_generation_start
from app.runtime_config import config_integer, config_number
from app.task_runtime.logging import log_message


def _state_name(file_state: Any) -> str:
    state = getattr(file_state, "state", None)
    return str(getattr(state, "name", None) or state or "").upper()


def _chunk_text(chunk: Any) -> str:
    parts: list[str] = []
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


def _stream_text(response: Any) -> str:
    return "".join(_chunk_text(chunk) for chunk in response)


async def _generate_with_response_deadline(client, *, model_name, contents, config, response_timeout_seconds):
    """Cancel stalled or continually streaming generation at one absolute deadline."""
    async with client.aio as async_client:
        try:
            async with asyncio.timeout(response_timeout_seconds):
                response = await async_client.models.generate_content_stream(
                    model=model_name, contents=contents, config=config,
                )
                try:
                    return "".join([_chunk_text(chunk) async for chunk in response])
                finally:
                    await response.aclose()
        except TimeoutError as exc:
            raise TimeoutError(f"Gemini response exceeded {response_timeout_seconds} seconds") from exc


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
    timeout_seconds: int,
    response_timeout_seconds: int | None = None,
) -> str:
    """Run one structured request and always delete uploaded Gemini files."""
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ValueError("Gemini timeout_seconds must be a positive integer")
    if response_timeout_seconds is not None and (
        type(response_timeout_seconds) is not int or not 1 <= response_timeout_seconds <= 900
    ):
        raise ValueError("Gemini response_timeout_seconds must be an integer from 1 to 900")
    client = genai.Client(api_key=api_key)
    uploaded: list[Any] = []
    try:
        for path, mime_type in (files or {}).items():
            item = client.files.upload(
                file=str(path),
                config={"mime_type": str(mime_type)},
            )
            uploaded.append(item)
            deadline = time.monotonic() + config_integer("gemini", "request", "file_activation_timeout_seconds")
            while _state_name(item) != "ACTIVE":
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Gemini file did not become active: {path}")
                time.sleep(config_number("gemini", "request", "file_poll_seconds", minimum=0.01))
                item = client.files.get(name=item.name)
                uploaded[-1] = item

        record_gemini_generation_start()
        config = types.GenerateContentConfig(
            temperature=config_number("gemini", "request", "temperature", maximum=2),
            response_mime_type="application/json",
            response_schema=_transport_response_schema(response_schema),
            candidate_count=1,
            seed=config_integer("gemini", "request", "seed", minimum=0),
            http_options=types.HttpOptions(
                timeout=(timeout_seconds if response_timeout_seconds is None else response_timeout_seconds) * 1000,
            ),
        )
        if response_timeout_seconds is not None:
            started = time.monotonic()
            log_message(f"Gemini response start model={model_name} timeout_seconds={response_timeout_seconds}")
            try:
                return asyncio.run(_generate_with_response_deadline(
                    client, model_name=model_name, contents=[*contents, *uploaded], config=config,
                    response_timeout_seconds=response_timeout_seconds,
                ))
            finally:
                log_message(f"Gemini response end model={model_name} elapsed_seconds={time.monotonic() - started:.1f}")
        response = client.models.generate_content_stream(
            model=model_name,
            contents=[*contents, *uploaded],
            config=config,
        )
        return _stream_text(response)
    finally:
        for item in uploaded:
            try:
                client.files.delete(name=item.name)
            except Exception as exc:  # noqa: BLE001
                log_message(
                    f"gemini request: uploaded file cleanup failed "
                    f"name={getattr(item, 'name', '')} error={type(exc).__name__}: {exc}",
                )
        client.close()


__all__ = ["generate_structured_json"]
