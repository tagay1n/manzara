"""Gemini structured request transport cleanup contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from app import gemini_requests


class _StrictNested(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class _StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nested: _StrictNested


def _contains_key(value, key):  # noqa: ANN001
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def test_strict_pydantic_schema_is_made_gemini_transport_safe(monkeypatch) -> None:
    captured = {}

    class Models:
        def generate_content_stream(self, **kwargs):  # noqa: ANN003
            captured["schema"] = kwargs["config"].response_schema
            return []

    client = SimpleNamespace(files=SimpleNamespace(), models=Models())
    monkeypatch.setattr(gemini_requests.genai, "Client", lambda **_kwargs: client)

    assert gemini_requests.generate_structured_json(
        api_key="key",
        model_name="model",
        contents=({"text": "prompt"},),
        response_schema=_StrictResponse,
    ) == ""

    assert isinstance(captured["schema"], dict)
    assert not _contains_key(captured["schema"], "additionalProperties")
    assert not _contains_key(captured["schema"], "additional_properties")
    with pytest.raises(ValueError):
        _StrictResponse.model_validate(
            {"nested": {"value": "ok", "unexpected": True}}
        )


def test_uploaded_file_is_deleted_when_generation_fails(monkeypatch, tmp_path) -> None:
    uploaded = SimpleNamespace(name="files/one", state="ACTIVE")
    deleted: list[str] = []

    class Files:
        def upload(self, **_kwargs):  # noqa: ANN003
            return uploaded

        def delete(self, *, name):  # noqa: ANN001
            deleted.append(name)

    class Models:
        def generate_content_stream(self, **_kwargs):  # noqa: ANN003
            raise RuntimeError("generation failed")

    client = SimpleNamespace(files=Files(), models=Models())
    monkeypatch.setattr(gemini_requests.genai, "Client", lambda **_kwargs: client)
    source = tmp_path / "slice.pdf"
    source.write_bytes(b"pdf")

    with pytest.raises(RuntimeError, match="generation failed"):
        gemini_requests.generate_structured_json(
            api_key="key",
            model_name="model",
            contents=({"text": "prompt"},),
            response_schema={"type": "object"},
            files={source: "application/pdf"},
        )

    assert deleted == ["files/one"]
