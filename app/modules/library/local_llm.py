"""Shared local-LLM configuration and Ollama transport for Library tasks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

from app.runtime_config import load_runtime_config


DEFAULT_OLLAMA_ENDPOINT = "http://127.0.0.1:11434"
DEFAULT_COLLECTION_TRIAGE_MODELS = ("qwen3:4b", "qwen3:8b")


@dataclass(frozen=True)
class LocalLLMSettings:
    """Runtime settings for local collection-triage inference."""

    endpoint: str
    collection_triage_models: tuple[str, ...]
    timeout_seconds: float


def load_local_llm_settings(config: Mapping[str, Any] | None = None) -> LocalLLMSettings:
    """Load local inference settings from the shared unmasked runtime config."""
    payload = dict(config) if config is not None else load_runtime_config()
    local = payload.get("local_llm")
    if not isinstance(local, Mapping):
        local = {}
    triage = local.get("collection_triage")
    if not isinstance(triage, Mapping):
        triage = {}

    endpoint = str(local.get("endpoint") or DEFAULT_OLLAMA_ENDPOINT).strip().rstrip("/")
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("local_llm.endpoint must be an absolute HTTP(S) URL")

    raw_models = triage.get("models", DEFAULT_COLLECTION_TRIAGE_MODELS)
    if not isinstance(raw_models, (list, tuple)):
        raise ValueError("local_llm.collection_triage.models must be a list")
    models = tuple(dict.fromkeys(str(item or "").strip() for item in raw_models if str(item or "").strip()))
    if not models:
        raise ValueError("local_llm.collection_triage.models must not be empty")

    raw_timeout = triage.get("timeout_seconds", 300)
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
        raise ValueError("local_llm.collection_triage.timeout_seconds must be numeric")
    timeout_seconds = float(raw_timeout)
    if timeout_seconds < 10 or timeout_seconds > 3600:
        raise ValueError("local_llm.collection_triage.timeout_seconds must be between 10 and 3600")

    return LocalLLMSettings(
        endpoint=endpoint,
        collection_triage_models=models,
        timeout_seconds=timeout_seconds,
    )


class OllamaClient:
    """Minimal Ollama adapter with deterministic structured-output requests."""

    def __init__(self, settings: LocalLLMSettings, *, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=settings.endpoint,
            timeout=httpx.Timeout(settings.timeout_seconds),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def preflight(self, models: tuple[str, ...]) -> None:
        """Fail once with actionable context when Ollama or a model is unavailable."""
        try:
            response = self._client.get("/api/tags")
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(
                f"Ollama is unavailable at {self.settings.endpoint}: {exc}"
            ) from exc
        rows = payload.get("models") if isinstance(payload, dict) else None
        available = {
            str((row or {}).get("name") or (row or {}).get("model") or "").strip()
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict)
        }
        missing = [model for model in models if model not in available]
        if missing:
            commands = ", ".join(f"ollama pull {model}" for model in missing)
            raise RuntimeError(f"Ollama model(s) missing: {', '.join(missing)}. Run: {commands}")

    def evaluate(self, *, model_name: str, prompt: str) -> dict[str, Any]:
        """Evaluate one prompt and return content plus Ollama timing metadata."""
        try:
            response = self._client.post(
                "/api/chat",
                json={
                    "model": model_name,
                    "stream": False,
                    "think": False,
                    "format": "json",
                    "options": {"temperature": 0, "seed": 0},
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text.replace("\n", " ")[:500]
            raise RuntimeError(
                f"Ollama request failed status={exc.response.status_code} body={body}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"Ollama request failed for model {model_name}: {exc}") from exc

        message = payload.get("message") if isinstance(payload, dict) else None
        content = str((message or {}).get("content") or "").strip()
        if not content:
            raise RuntimeError(f"Ollama returned empty content for model {model_name}")
        total_duration = payload.get("total_duration") if isinstance(payload, dict) else None
        latency_ms = round(float(total_duration or 0) / 1_000_000) if total_duration else 0
        return {
            "content": content,
            "latency_ms": latency_ms,
            "prompt_tokens": int(payload.get("prompt_eval_count") or 0),
            "output_tokens": int(payload.get("eval_count") or 0),
        }
