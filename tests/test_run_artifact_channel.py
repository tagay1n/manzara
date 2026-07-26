"""Tests for structured run artifact channel helpers."""

from __future__ import annotations

from pathlib import Path

from app.run_artifact_channel import emit_run_artifact, read_run_artifact


def test_emit_and_read_run_artifact(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "task_runs" / "t1" / "run-1.artifact.json"
    monkeypatch.setenv("MANZARA_RUN_ARTIFACT_PATH", str(target))
    ok = emit_run_artifact({"kind": "test.summary", "items_processed": 2})
    assert ok is True
    payload = read_run_artifact(target)
    assert payload["kind"] == "test.summary"
    assert int(payload["items_processed"]) == 2


def test_emit_run_artifact_returns_false_when_env_is_missing(monkeypatch) -> None:
    monkeypatch.delenv("MANZARA_RUN_ARTIFACT_PATH", raising=False)
    assert emit_run_artifact({"kind": "x"}) is False
