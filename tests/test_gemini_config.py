"""Gemini config loader policy tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app import gemini_config


def test_candidate_paths_exclude_config_example(monkeypatch) -> None:
    monkeypatch.delenv("MANZARA_CONFIG_PATH", raising=False)
    names = [path.name for path in gemini_config._candidate_config_paths()]
    assert "config.example.yaml" not in names


def test_shared_model_pool_is_required(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text("gemini:\n  accounts: {}\n", encoding="utf-8")

    monkeypatch.setenv("MANZARA_CONFIG_PATH", str(config_path))

    with pytest.raises(RuntimeError, match="gemini.model_pool is required"):
        gemini_config.load_required_gemini_model_pool()


def test_shared_model_pool_preserves_order_and_removes_duplicates(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "gemini": {
                    "model_pool": ["model-a", "model-b", "model-a"]
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MANZARA_CONFIG_PATH", str(config_path))

    assert gemini_config.load_required_gemini_model_pool() == [
        "model-a",
        "model-b",
    ]


def test_configured_model_names_come_only_from_shared_pool(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "gemini": {
                    "model_pool": ["model-shared", "model-fallback"],
                    "models": {"legacy-alias": "model-must-not-appear"},
                    "model_pools": {
                        "legacy-operation": ["legacy-model"]
                    },
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MANZARA_CONFIG_PATH", str(config_path))

    assert gemini_config.load_configured_gemini_model_names() == [
        "model-shared",
        "model-fallback",
    ]


def test_normalization_uses_full_shared_ordered_model_pool(monkeypatch) -> None:
    from app.modules.library import normalization

    monkeypatch.setattr(
        normalization,
        "load_required_gemini_model_pool",
        lambda: ["model-first", "model-fallback"],
    )

    captured = {}

    def fake_pool(**kwargs):  # noqa: ANN003
        captured["models"] = kwargs["models"]
        return SimpleNamespace(
            model_name="model-fallback",
            value={
                "suggestion_kind": "create",
                "target_canonical_id": None,
                "confidence": 0.8,
                "confidence_band": "medium",
                "rationale": "test",
            },
        )

    monkeypatch.setattr(normalization, "run_ordered_model_pool", fake_pool)
    result = normalization._gemini_suggest(
        entity_type="publisher",
        raw_name="Publisher",
        normalized_name="publisher",
        docs_count=2,
        mentions_count=2,
        marker_count=1,
        canonical_candidates=[],
        manager=object(),
    )

    assert captured["models"] == ["model-first", "model-fallback"]
    assert result["model"] == "model-fallback"
