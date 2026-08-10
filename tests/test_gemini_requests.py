"""Gemini structured request transport cleanup contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import gemini_requests


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
