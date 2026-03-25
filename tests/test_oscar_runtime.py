"""Tests for Oscar stage runtime runner."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.modules.oscar.runtime import run_stage


class _FakeDb:
    def __init__(self, snapshot: str | None):
        self._snapshot = snapshot
        self.stage_updates: list[tuple[str, str, str, int | None, str | None]] = []
        self.snapshot_updates: list[tuple[str, str, str | None]] = []

    def claim_next_oscar_snapshot(self):
        if not self._snapshot:
            return None
        return {"snapshot_id": self._snapshot, "status": "processing"}

    def get_oscar_snapshot(self, snapshot_id: str):
        if self._snapshot and snapshot_id == self._snapshot:
            return {"snapshot_id": snapshot_id, "status": "processing"}
        return None

    def upsert_oscar_snapshot(
        self,
        snapshot_id: str,
        *,
        source_path: str | None = None,
        source_label: str | None = None,
        metadata: dict | None = None,
        status: str = "pending",
        discovered_at: str | None = None,
    ) -> None:
        _ = (source_path, source_label, metadata, status, discovered_at)
        if not self._snapshot:
            self._snapshot = snapshot_id

    def upsert_oscar_snapshot_stage(
        self,
        snapshot_id: str,
        stage_name: str,
        status: str,
        *,
        run_id: int | None = None,
        error_text: str | None = None,
    ) -> None:
        self.stage_updates.append((snapshot_id, stage_name, status, run_id, error_text))

    def set_oscar_snapshot_status(
        self,
        snapshot_id: str,
        status: str,
        *,
        error_text: str | None = None,
    ) -> None:
        self.snapshot_updates.append((snapshot_id, status, error_text))


def test_resolve_offsets_stage_success_claims_snapshot_and_updates_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_db = _FakeDb(snapshot="CC-MAIN-2024-10")
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        run_stage,
        "load_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg2://user:pass@localhost:5432/monocorpus",
            database_schema="monocorpus",
        ),
    )
    monkeypatch.setattr(run_stage, "Database", lambda *_args, **_kwargs: fake_db)

    def _fake_run(cmd, *, cwd, env, text, check):  # noqa: ANN001
        calls.append({"cmd": cmd, "cwd": cwd, "env": env, "text": text, "check": check})
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(run_stage.subprocess, "run", _fake_run)

    repo = tmp_path / "oscar-corpus-extractor"
    artifacts = tmp_path / "artifacts"
    repo.mkdir(parents=True, exist_ok=True)

    code = run_stage.main(
        [
            "--stage",
            "resolve_offsets_local",
            "--repo-path",
            str(repo),
            "--artifacts-dir",
            str(artifacts),
        ]
    )
    assert code == 0
    assert len(calls) == 1
    cmd = list(calls[0]["cmd"])
    assert cmd[-2:] == ["--snapshot", "CC-MAIN-2024-10"]
    assert "resolve-offsets-local" in cmd
    assert calls[0]["cwd"] == str(repo)
    env = dict(calls[0]["env"])
    assert env["OSCAR_APP_DIR"] == str(artifacts)

    assert fake_db.stage_updates[0][:3] == (
        "CC-MAIN-2024-10",
        "resolve_offsets_local",
        "running",
    )
    assert fake_db.stage_updates[1][:3] == (
        "CC-MAIN-2024-10",
        "resolve_offsets_local",
        "completed",
    )
    assert fake_db.snapshot_updates[-1] == ("CC-MAIN-2024-10", "processing", None)


def test_resolve_offsets_stage_failure_marks_snapshot_failed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_db = _FakeDb(snapshot="CC-MAIN-2024-11")

    monkeypatch.setattr(
        run_stage,
        "load_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg2://user:pass@localhost:5432/monocorpus",
            database_schema="monocorpus",
        ),
    )
    monkeypatch.setattr(run_stage, "Database", lambda *_args, **_kwargs: fake_db)
    monkeypatch.setattr(
        run_stage.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout="", stderr="resolver failed"),
    )

    repo = tmp_path / "oscar-corpus-extractor"
    artifacts = tmp_path / "artifacts"
    repo.mkdir(parents=True, exist_ok=True)

    code = run_stage.main(
        [
            "--stage",
            "resolve_offsets_local",
            "--repo-path",
            str(repo),
            "--artifacts-dir",
            str(artifacts),
        ]
    )
    assert code == 1
    assert fake_db.stage_updates[0][:3] == (
        "CC-MAIN-2024-11",
        "resolve_offsets_local",
        "running",
    )
    assert fake_db.stage_updates[1][:3] == (
        "CC-MAIN-2024-11",
        "resolve_offsets_local",
        "failed",
    )
    assert "resolver failed" in str(fake_db.stage_updates[1][4] or "")
    assert fake_db.snapshot_updates[-1][0] == "CC-MAIN-2024-11"
    assert fake_db.snapshot_updates[-1][1] == "failed"


def test_resolve_offsets_stage_exits_cleanly_when_no_pending_snapshots(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_db = _FakeDb(snapshot=None)
    calls: list[object] = []

    monkeypatch.setattr(
        run_stage,
        "load_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg2://user:pass@localhost:5432/monocorpus",
            database_schema="monocorpus",
        ),
    )
    monkeypatch.setattr(run_stage, "Database", lambda *_args, **_kwargs: fake_db)
    monkeypatch.setattr(run_stage.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    repo = tmp_path / "oscar-corpus-extractor"
    artifacts = tmp_path / "artifacts"
    repo.mkdir(parents=True, exist_ok=True)

    code = run_stage.main(
        [
            "--stage",
            "resolve_offsets_local",
            "--repo-path",
            str(repo),
            "--artifacts-dir",
            str(artifacts),
        ]
    )
    assert code == 0
    assert calls == []
    assert fake_db.stage_updates == []
    assert fake_db.snapshot_updates == []
