"""Gemini config loader policy tests."""

from __future__ import annotations

from pathlib import Path

import yaml

from app import gemini_config


def test_candidate_paths_exclude_config_example(monkeypatch) -> None:
    monkeypatch.delenv("MANZARA_CONFIG_PATH", raising=False)
    names = [path.name for path in gemini_config._candidate_config_paths()]
    assert "config.example.yaml" not in names


def test_load_gemini_models_merges_overrides(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "gemini": {
                    "models": {
                        "library_normalization": "gemini-2.5-flash-lite",
                    }
                }
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("MANZARA_CONFIG_PATH", str(config_path))
    models = gemini_config.load_gemini_models()

    assert models["library_normalization"] == "gemini-2.5-flash-lite"
    assert models["library_meta_evaluate"] == gemini_config.DEFAULT_GEMINI_MODELS["library_meta_evaluate"]
