from __future__ import annotations

import json

import httpx
import pytest

from app.modules.library.local_llm import (
    LocalLLMSettings,
    OllamaClient,
    load_local_llm_settings,
)


def test_local_llm_settings_load_configured_models() -> None:
    settings = load_local_llm_settings(
        {
            "local_llm": {
                "endpoint": "http://localhost:11434/",
                "collection_triage": {
                    "models": ["qwen3:4b", "qwen3:8b", "qwen3:4b"],
                    "timeout_seconds": 240,
                },
            }
        }
    )

    assert settings.endpoint == "http://localhost:11434"
    assert settings.collection_triage_models == ("qwen3:4b", "qwen3:8b")
    assert settings.timeout_seconds == 240


def test_local_llm_settings_reject_ambiguous_timeout() -> None:
    with pytest.raises(ValueError, match="must be numeric"):
        load_local_llm_settings(
            {"local_llm": {"collection_triage": {"timeout_seconds": True}}}
        )


def test_ollama_preflight_reports_missing_models() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"models": [{"name": "qwen3:4b"}]})
    )
    http_client = httpx.Client(base_url="http://localhost:11434", transport=transport)
    client = OllamaClient(
        LocalLLMSettings("http://localhost:11434", ("qwen3:4b", "qwen3:8b"), 300),
        client=http_client,
    )

    with pytest.raises(RuntimeError, match=r"ollama pull qwen3:8b"):
        client.preflight(("qwen3:4b", "qwen3:8b"))


def test_ollama_evaluate_disables_thinking_and_streaming() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "message": {"content": '{"verdict":"uncertain"}'},
                "total_duration": 12_000_000,
                "prompt_eval_count": 25,
                "eval_count": 6,
            },
        )

    http_client = httpx.Client(
        base_url="http://localhost:11434",
        transport=httpx.MockTransport(handler),
    )
    client = OllamaClient(
        LocalLLMSettings("http://localhost:11434", ("qwen3:4b",), 300),
        client=http_client,
    )

    result = client.evaluate(model_name="qwen3:4b", prompt="evidence")

    assert captured["think"] is False
    assert captured["stream"] is False
    assert captured["format"] == "json"
    assert captured["options"] == {"temperature": 0, "seed": 0}
    assert result["latency_ms"] == 12
