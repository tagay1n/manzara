"""Gemini config loader policy tests."""

from __future__ import annotations

from pathlib import Path

import pytest
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


def test_load_collection_validation_model_pool(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "gemini": {
                    "model_pools": {
                        "library_collection_validation": [
                            "model-a",
                            "model-b",
                            "model-a",
                        ]
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MANZARA_CONFIG_PATH", str(config_path))

    pools = gemini_config.load_gemini_model_pools()

    assert pools["library_collection_validation"] == ["model-a", "model-b"]


def test_required_model_pool_has_no_implicit_default(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text("gemini:\n  accounts: {}\n", encoding="utf-8")
    monkeypatch.setenv("MANZARA_CONFIG_PATH", str(config_path))

    with pytest.raises(RuntimeError, match="library_metadata_extraction"):
        gemini_config.load_required_gemini_model_pool(
            "library_metadata_extraction"
        )


def test_required_model_pool_preserves_configured_order(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "gemini": {
                    "model_pools": {
                        "library_metadata_extraction": [
                            "model-first",
                            "model-second",
                            "model-first",
                        ]
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MANZARA_CONFIG_PATH", str(config_path))

    assert gemini_config.load_required_gemini_model_pool(
        "library_metadata_extraction"
    ) == ["model-first", "model-second"]


def test_required_metadata_evaluation_pool_preserves_order(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "gemini": {
                    "model_pools": {
                        "library_metadata_evaluation": [
                            "model-newest",
                            "model-fallback",
                        ]
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MANZARA_CONFIG_PATH", str(config_path))

    assert gemini_config.load_required_gemini_model_pool(
        "library_metadata_evaluation"
    ) == ["model-newest", "model-fallback"]
