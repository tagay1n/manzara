"""Tests for Oscar stage runtime runner."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.modules.oscar.runtime import run_stage


class _FakeDb:
    def __init__(self, snapshot: str | None, *, stage_snapshot: str | None = None):
        self._snapshot = snapshot
        self._stage_snapshot = stage_snapshot
        self.stage_updates: list[tuple[str, str, str, int | None, str | None]] = []
        self.snapshot_updates: list[tuple[str, str, str | None]] = []
        self.stage_claim_args: list[tuple[str, str | None, tuple[str, ...] | None]] = []

    def claim_next_oscar_snapshot(self):
        if not self._snapshot:
            return None
        return {"snapshot_id": self._snapshot, "status": "processing"}

    def claim_next_oscar_snapshot_for_stage(
        self,
        stage_name: str,
        *,
        required_stage: str | None = None,
        allowed_snapshot_statuses: list[str] | None = None,
    ):
        statuses = tuple(allowed_snapshot_statuses or [])
        self.stage_claim_args.append((stage_name, required_stage, statuses or None))
        if not self._stage_snapshot:
            return None
        return {"snapshot_id": self._stage_snapshot, "status": "processing"}

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


def test_download_ranges_stage_success_uses_stage_claim_and_updates_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_db = _FakeDb(snapshot=None, stage_snapshot="CC-MAIN-2024-20")
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
            "download_ranges",
            "--repo-path",
            str(repo),
            "--artifacts-dir",
            str(artifacts),
        ]
    )
    assert code == 0
    assert fake_db.stage_claim_args == [("download_ranges", "resolve_offsets_local", None)]
    assert len(calls) == 1
    cmd = list(calls[0]["cmd"])
    assert "download-ranges" in cmd
    assert cmd[-2:] == ["--snapshot", "CC-MAIN-2024-20"]
    assert fake_db.stage_updates[0][:3] == ("CC-MAIN-2024-20", "download_ranges", "running")
    assert fake_db.stage_updates[1][:3] == ("CC-MAIN-2024-20", "download_ranges", "completed")
    assert fake_db.snapshot_updates[-1] == ("CC-MAIN-2024-20", "processing", None)


def test_download_ranges_stage_failure_marks_snapshot_failed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_db = _FakeDb(snapshot=None, stage_snapshot="CC-MAIN-2024-21")

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
        lambda *args, **kwargs: SimpleNamespace(returncode=9, stdout="", stderr="download failed"),
    )

    repo = tmp_path / "oscar-corpus-extractor"
    artifacts = tmp_path / "artifacts"
    repo.mkdir(parents=True, exist_ok=True)

    code = run_stage.main(
        [
            "--stage",
            "download_ranges",
            "--repo-path",
            str(repo),
            "--artifacts-dir",
            str(artifacts),
        ]
    )
    assert code == 1
    assert fake_db.stage_updates[0][:3] == ("CC-MAIN-2024-21", "download_ranges", "running")
    assert fake_db.stage_updates[1][:3] == ("CC-MAIN-2024-21", "download_ranges", "failed")
    assert "download failed" in str(fake_db.stage_updates[1][4] or "")
    assert fake_db.snapshot_updates[-1][0] == "CC-MAIN-2024-21"
    assert fake_db.snapshot_updates[-1][1] == "failed"


def test_download_ranges_stage_exits_cleanly_when_no_ready_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_db = _FakeDb(snapshot=None, stage_snapshot=None)
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
            "download_ranges",
            "--repo-path",
            str(repo),
            "--artifacts-dir",
            str(artifacts),
        ]
    )
    assert code == 0
    assert fake_db.stage_claim_args == [("download_ranges", "resolve_offsets_local", None)]
    assert calls == []
    assert fake_db.stage_updates == []
    assert fake_db.snapshot_updates == []


def test_export_parquet_stage_success_marks_snapshot_completed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_db = _FakeDb(snapshot=None, stage_snapshot="CC-MAIN-2024-30")
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
            "export_parquet",
            "--repo-path",
            str(repo),
            "--artifacts-dir",
            str(artifacts),
            "--part-size-mb",
            "1024",
        ]
    )
    assert code == 0
    assert fake_db.stage_claim_args == [("export_parquet", "download_ranges", None)]
    assert len(calls) == 1
    cmd = list(calls[0]["cmd"])
    assert "export-parquet" in cmd
    assert "--split" in cmd
    assert "1024" in cmd
    assert "--snapshot" in cmd
    snapshot_idx = cmd.index("--snapshot")
    assert cmd[snapshot_idx + 1] == "CC-MAIN-2024-30"
    assert fake_db.stage_updates[0][:3] == ("CC-MAIN-2024-30", "export_parquet", "running")
    assert fake_db.stage_updates[1][:3] == ("CC-MAIN-2024-30", "export_parquet", "completed")
    assert fake_db.snapshot_updates[-1] == ("CC-MAIN-2024-30", "completed", None)


def test_export_parquet_stage_failure_marks_snapshot_failed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_db = _FakeDb(snapshot=None, stage_snapshot="CC-MAIN-2024-31")

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
        lambda *args, **kwargs: SimpleNamespace(returncode=7, stdout="", stderr="export failed"),
    )

    repo = tmp_path / "oscar-corpus-extractor"
    artifacts = tmp_path / "artifacts"
    repo.mkdir(parents=True, exist_ok=True)

    code = run_stage.main(
        [
            "--stage",
            "export_parquet",
            "--repo-path",
            str(repo),
            "--artifacts-dir",
            str(artifacts),
            "--part-size-mb",
            "1024",
        ]
    )
    assert code == 1
    assert fake_db.stage_updates[0][:3] == ("CC-MAIN-2024-31", "export_parquet", "running")
    assert fake_db.stage_updates[1][:3] == ("CC-MAIN-2024-31", "export_parquet", "failed")
    assert "export failed" in str(fake_db.stage_updates[1][4] or "")
    assert fake_db.snapshot_updates[-1][0] == "CC-MAIN-2024-31"
    assert fake_db.snapshot_updates[-1][1] == "failed"


def test_export_parquet_stage_exits_cleanly_when_no_ready_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_db = _FakeDb(snapshot=None, stage_snapshot=None)
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
            "export_parquet",
            "--repo-path",
            str(repo),
            "--artifacts-dir",
            str(artifacts),
        ]
    )
    assert code == 0
    assert fake_db.stage_claim_args == [("export_parquet", "download_ranges", None)]


def test_discover_snapshots_stage_runs_ingest_command(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_db = _FakeDb(snapshot=None, stage_snapshot=None)
    calls: list[dict[str, object]] = []
    seeded: list[Path] = []

    monkeypatch.setattr(
        run_stage,
        "load_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg2://user:pass@localhost:5432/monocorpus",
            database_schema="monocorpus",
        ),
    )
    monkeypatch.setattr(run_stage, "Database", lambda *_args, **_kwargs: fake_db)

    def _fake_seed(*, db, oscar_app_dir):  # noqa: ANN001
        _ = db
        seeded.append(Path(oscar_app_dir))

    def _fake_run(cmd, *, cwd, env, text, check):  # noqa: ANN001
        calls.append({"cmd": cmd, "cwd": cwd, "env": env, "text": text, "check": check})
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(run_stage, "_seed_snapshot_queue_from_sqlite", _fake_seed)
    monkeypatch.setattr(run_stage.subprocess, "run", _fake_run)

    repo = tmp_path / "oscar-corpus-extractor"
    artifacts = tmp_path / "artifacts"
    repo.mkdir(parents=True, exist_ok=True)

    code = run_stage.main(
        [
            "--stage",
            "discover_snapshots",
            "--repo-path",
            str(repo),
            "--artifacts-dir",
            str(artifacts),
        ]
    )
    assert code == 0
    assert len(calls) == 1
    cmd = list(calls[0]["cmd"])
    assert cmd[:3] == ["python3", "-m", "app.cli"]
    assert cmd[-1] == "ingest"
    assert calls[0]["cwd"] == str(repo)
    env = dict(calls[0]["env"])
    assert env["OSCAR_APP_DIR"] == str(artifacts)
    assert seeded == [artifacts, artifacts]


def test_discover_snapshots_stage_failure_returns_nonzero(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_db = _FakeDb(snapshot=None, stage_snapshot=None)

    monkeypatch.setattr(
        run_stage,
        "load_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg2://user:pass@localhost:5432/monocorpus",
            database_schema="monocorpus",
        ),
    )
    monkeypatch.setattr(run_stage, "Database", lambda *_args, **_kwargs: fake_db)
    monkeypatch.setattr(run_stage, "_seed_snapshot_queue_from_sqlite", lambda **_kwargs: None)
    monkeypatch.setattr(
        run_stage.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=4, stdout="", stderr="ingest failed"),
    )

    repo = tmp_path / "oscar-corpus-extractor"
    artifacts = tmp_path / "artifacts"
    repo.mkdir(parents=True, exist_ok=True)

    code = run_stage.main(
        [
            "--stage",
            "discover_snapshots",
            "--repo-path",
            str(repo),
            "--artifacts-dir",
            str(artifacts),
        ]
    )
    assert code == 1
    assert fake_db.stage_updates == []
    assert fake_db.snapshot_updates == []


def test_upload_dataset_stage_success_claims_completed_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_db = _FakeDb(snapshot=None, stage_snapshot="CC-MAIN-2024-60")
    uploads: list[tuple[str, Path]] = []

    monkeypatch.setattr(
        run_stage,
        "load_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg2://user:pass@localhost:5432/monocorpus",
            database_schema="monocorpus",
        ),
    )
    monkeypatch.setattr(run_stage, "Database", lambda *_args, **_kwargs: fake_db)
    monkeypatch.setattr(run_stage, "_seed_snapshot_queue_from_sqlite", lambda **_kwargs: None)

    def _fake_upload(*, snapshot_id: str, oscar_app_dir: Path, source_repo: Path) -> dict[str, object]:
        _ = source_repo
        uploads.append((snapshot_id, oscar_app_dir))
        return {"uploaded_files": 1, "repo_id": "owner/repo"}

    monkeypatch.setattr(run_stage, "_upload_snapshot_dataset", _fake_upload)

    repo = tmp_path / "oscar-corpus-extractor"
    artifacts = tmp_path / "artifacts"
    repo.mkdir(parents=True, exist_ok=True)

    code = run_stage.main(
        [
            "--stage",
            "upload_dataset",
            "--repo-path",
            str(repo),
            "--artifacts-dir",
            str(artifacts),
        ]
    )
    assert code == 0
    assert fake_db.stage_claim_args == [("upload_dataset", "export_parquet", ("completed",))]
    assert fake_db.stage_updates[0][:3] == ("CC-MAIN-2024-60", "upload_dataset", "running")
    assert fake_db.stage_updates[1][:3] == ("CC-MAIN-2024-60", "upload_dataset", "completed")
    assert fake_db.snapshot_updates[-1] == ("CC-MAIN-2024-60", "completed", None)
    assert uploads == [("CC-MAIN-2024-60", artifacts)]


def test_upload_dataset_stage_exits_cleanly_when_no_ready_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_db = _FakeDb(snapshot=None, stage_snapshot=None)
    uploads: list[object] = []

    monkeypatch.setattr(
        run_stage,
        "load_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg2://user:pass@localhost:5432/monocorpus",
            database_schema="monocorpus",
        ),
    )
    monkeypatch.setattr(run_stage, "Database", lambda *_args, **_kwargs: fake_db)
    monkeypatch.setattr(run_stage, "_seed_snapshot_queue_from_sqlite", lambda **_kwargs: None)
    monkeypatch.setattr(run_stage, "_upload_snapshot_dataset", lambda **kwargs: uploads.append(kwargs))

    repo = tmp_path / "oscar-corpus-extractor"
    artifacts = tmp_path / "artifacts"
    repo.mkdir(parents=True, exist_ok=True)

    code = run_stage.main(
        [
            "--stage",
            "upload_dataset",
            "--repo-path",
            str(repo),
            "--artifacts-dir",
            str(artifacts),
        ]
    )
    assert code == 0
    assert fake_db.stage_claim_args == [("upload_dataset", "export_parquet", ("completed",))]
    assert uploads == []
    assert fake_db.stage_updates == []
    assert fake_db.snapshot_updates == []


def test_upload_dataset_stage_failure_marks_snapshot_failed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_db = _FakeDb(snapshot=None, stage_snapshot="CC-MAIN-2024-61")

    monkeypatch.setattr(
        run_stage,
        "load_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg2://user:pass@localhost:5432/monocorpus",
            database_schema="monocorpus",
        ),
    )
    monkeypatch.setattr(run_stage, "Database", lambda *_args, **_kwargs: fake_db)
    monkeypatch.setattr(run_stage, "_seed_snapshot_queue_from_sqlite", lambda **_kwargs: None)

    def _fake_upload(*, snapshot_id: str, oscar_app_dir: Path, source_repo: Path) -> dict[str, object]:
        _ = (snapshot_id, oscar_app_dir, source_repo)
        raise RuntimeError("upload failed")

    monkeypatch.setattr(run_stage, "_upload_snapshot_dataset", _fake_upload)

    repo = tmp_path / "oscar-corpus-extractor"
    artifacts = tmp_path / "artifacts"
    repo.mkdir(parents=True, exist_ok=True)

    code = run_stage.main(
        [
            "--stage",
            "upload_dataset",
            "--repo-path",
            str(repo),
            "--artifacts-dir",
            str(artifacts),
        ]
    )
    assert code == 1
    assert fake_db.stage_updates[0][:3] == ("CC-MAIN-2024-61", "upload_dataset", "running")
    assert fake_db.stage_updates[1][:3] == ("CC-MAIN-2024-61", "upload_dataset", "failed")
    assert "upload failed" in str(fake_db.stage_updates[1][4] or "")
    assert fake_db.snapshot_updates[-1][0] == "CC-MAIN-2024-61"
    assert fake_db.snapshot_updates[-1][1] == "failed"
