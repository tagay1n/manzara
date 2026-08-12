"""Tests for Shayan runtime stage runner."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.modules.shayan.runtime import run_stage


class _FakeDb:
    def __init__(self) -> None:
        self.latest_snapshot_entries: dict[str, dict] = {}
        self.latest_manifest: dict[str, dict] = {}
        self.created_snapshots: list[dict] = []
        self.replaced_manifests: list[dict] = []
        self.replaced_changes: list[tuple[int, list[dict]]] = []

    def get_latest_shayan_snapshot_entries(self):
        return dict(self.latest_snapshot_entries)

    def create_shayan_snapshot(
        self,
        entries,
        *,
        run_id=None,
        source=None,
        generated_at=None,
    ):
        self.created_snapshots.append(
            {
                "entries": dict(entries),
                "run_id": run_id,
                "source": source,
                "generated_at": generated_at,
            }
        )
        return 77

    def list_shayan_manifest_entries(self):
        return dict(self.latest_manifest)

    def replace_shayan_manifest_entries(self, entries):
        payload = dict(entries)
        self.replaced_manifests.append(payload)
        self.latest_manifest = payload
        return len(payload)

    def replace_shayan_run_changes(self, run_id, changes):  # noqa: ANN001
        normalized = [dict(item) for item in changes]
        self.replaced_changes.append((int(run_id), normalized))
        return len(normalized)

def test_scan_stage_stores_snapshot_and_emits_artifacts(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.delenv("MANZARA_TASK_RUN_ID", raising=False)
    artifact_path = tmp_path / "run-artifact.json"
    monkeypatch.setenv("MANZARA_RUN_ARTIFACT_PATH", str(artifact_path))
    fake_db = _FakeDb()
    fake_db.latest_snapshot_entries = {"ep-1": {"title": "Episode 1"}}

    monkeypatch.setattr(
        run_stage,
        "load_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg2://user:pass@localhost:5432/monocorpus",
            database_schema="monocorpus",
        ),
    )
    monkeypatch.setattr(run_stage, "Database", lambda *_args, **_kwargs: fake_db)

    def _fake_run(command, *, cwd):  # noqa: ANN001
        _ = cwd
        output_file = Path(command[command.index("--output-file") + 1])
        output_file.write_text(
            json.dumps(
                {
                    "generated_at": "2026-03-26T10:00:00+00:00",
                    "source": "https://tt.shayantv.ru",
                    "entries": {
                        "ep-1": {"title": "Episode 1 updated"},
                        "ep-2": {"title": "Episode 2"},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return 0, ["scan-ok"]

    monkeypatch.setattr(run_stage, "_run_command_streaming", _fake_run)

    repo = tmp_path / "shayan-video-downloader"
    repo.mkdir(parents=True, exist_ok=True)
    output = tmp_path / "output"
    code = run_stage.main(
        [
            "--stage",
            "scan_changes",
            "--repo-path",
            str(repo),
            "--output-path",
            str(output),
        ]
    )
    assert code == 0
    assert len(fake_db.created_snapshots) == 1
    snapshot = fake_db.created_snapshots[0]
    assert snapshot["source"] == "https://tt.shayantv.ru"
    assert len(snapshot["entries"]) == 2
    assert fake_db.replaced_changes == []

    _ = capsys.readouterr().out
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["kind"] == "shayan.snapshot_diff"
    assert int(payload["episodes_added"]) == 1


def test_scan_stage_persists_detailed_changes_when_run_id_is_available(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_db = _FakeDb()
    fake_db.latest_snapshot_entries = {
        "ep-1": {"category": "cartoons", "program": "Alpha", "season": 1, "episode": 1, "title": "One"}
    }

    monkeypatch.setenv("MANZARA_TASK_RUN_ID", "42")
    monkeypatch.setattr(
        run_stage,
        "load_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg2://user:pass@localhost:5432/monocorpus",
            database_schema="monocorpus",
        ),
    )
    monkeypatch.setattr(run_stage, "Database", lambda *_args, **_kwargs: fake_db)

    def _fake_run(command, *, cwd):  # noqa: ANN001
        _ = cwd
        output_file = Path(command[command.index("--output-file") + 1])
        output_file.write_text(
            json.dumps(
                {
                    "generated_at": "2026-03-26T10:00:00+00:00",
                    "source": "https://tt.shayantv.ru",
                    "entries": {
                        "ep-1": {"category": "cartoons", "program": "Alpha", "season": 1, "episode": 1, "title": "One updated"},
                        "ep-2": {"category": "cartoons", "program": "Beta", "season": 2, "episode": 3, "title": "Three"},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return 0, ["scan-ok"]

    monkeypatch.setattr(run_stage, "_run_command_streaming", _fake_run)

    repo = tmp_path / "shayan-video-downloader"
    repo.mkdir(parents=True, exist_ok=True)
    output = tmp_path / "output"
    code = run_stage.main(
        [
            "--stage",
            "scan_changes",
            "--repo-path",
            str(repo),
            "--output-path",
            str(output),
        ]
    )
    assert code == 0
    assert len(fake_db.replaced_changes) == 1
    run_id, rows = fake_db.replaced_changes[0]
    assert run_id == 42
    by_type = {row["change_type"] for row in rows}
    assert by_type == {"added", "changed"}


def test_scan_stage_fails_when_snapshot_is_empty(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_db = _FakeDb()
    fake_db.latest_snapshot_entries = {"ep-1": {"title": "Episode 1"}}

    monkeypatch.setattr(
        run_stage,
        "load_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg2://user:pass@localhost:5432/monocorpus",
            database_schema="monocorpus",
        ),
    )
    monkeypatch.setattr(run_stage, "Database", lambda *_args, **_kwargs: fake_db)

    def _fake_run(command, *, cwd):  # noqa: ANN001
        _ = cwd
        output_file = Path(command[command.index("--output-file") + 1])
        output_file.write_text(
            json.dumps(
                {
                    "generated_at": "2026-03-26T10:00:00+00:00",
                    "source": "https://tt.shayantv.ru",
                    "entries": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return 0, ["scan-ok"]

    monkeypatch.setattr(run_stage, "_run_command_streaming", _fake_run)

    repo = tmp_path / "shayan-video-downloader"
    repo.mkdir(parents=True, exist_ok=True)
    output = tmp_path / "output"
    code = run_stage.main(
        [
            "--stage",
            "scan_changes",
            "--repo-path",
            str(repo),
            "--output-path",
            str(output),
        ]
    )
    assert code == 1
    assert fake_db.created_snapshots == []


def test_best_effort_snapshot_runner_uses_inline_python_with_repo_cwd(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def _fake_run(command, *, cwd):  # noqa: ANN001
        captured["command"] = list(command)
        captured["cwd"] = str(cwd)
        return 0, []

    monkeypatch.setattr(run_stage, "_run_command_streaming", _fake_run)
    monkeypatch.setattr(run_stage, "_python_bin", lambda _repo: "python3")

    snapshot = tmp_path / "latest.json"
    repo = tmp_path / "shayan-video-downloader"
    repo.mkdir(parents=True, exist_ok=True)

    code, lines = run_stage._run_best_effort_snapshot(
        repo_path=repo,
        snapshot_file=snapshot,
        category="all",
    )

    assert code == 0
    assert lines == []
    assert captured["cwd"] == str(repo)
    command = captured["command"]
    assert isinstance(command, list)
    assert command[1] == "-c"
    assert "from app.main import" in str(command[2])
    assert "--output-file" in command
    assert str(snapshot) in command


def test_download_stage_replaces_manifest_and_emits_summary_metrics(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    artifact_path = tmp_path / "run-artifact.json"
    monkeypatch.setenv("MANZARA_RUN_ARTIFACT_PATH", str(artifact_path))
    fake_db = _FakeDb()
    fake_db.latest_manifest = {
        "ep-1": {"title": "Episode 1", "file": "videos/ep1.mkv"},
    }

    monkeypatch.setattr(
        run_stage,
        "load_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg2://user:pass@localhost:5432/monocorpus",
            database_schema="monocorpus",
        ),
    )
    monkeypatch.setattr(run_stage, "Database", lambda *_args, **_kwargs: fake_db)

    def _fake_run(command, *, cwd):  # noqa: ANN001
        _ = cwd
        status_file = Path(command[command.index("--status-file") + 1])
        status_file.write_text(
            json.dumps(
                {
                    "ep-1": {"title": "Episode 1", "file": "videos/ep1.mkv"},
                    "ep-2": {"title": "Episode 2", "file": "videos/ep2.mkv"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return (
            0,
            [
                "episodes: seen 2 | listed 2 | downloaded 1 | skip-manifest 1 | skip-existing 0 | failed 0 | retries-used 0"
            ],
        )

    monkeypatch.setattr(run_stage, "_run_command_streaming", _fake_run)

    repo = tmp_path / "shayan-video-downloader"
    repo.mkdir(parents=True, exist_ok=True)
    output = tmp_path / "output"
    code = run_stage.main(
        [
            "--stage",
            "download_new",
            "--repo-path",
            str(repo),
            "--output-path",
            str(output),
        ]
    )
    assert code == 0
    assert len(fake_db.replaced_manifests) == 1
    assert len(fake_db.replaced_manifests[0]) == 2

    _ = capsys.readouterr().out
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["kind"] == "shayan.download_summary"
    assert int(payload["downloaded"]) == 1


def test_download_stage_fails_when_updated_manifest_is_empty(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_db = _FakeDb()
    fake_db.latest_manifest = {
        "ep-1": {"title": "Episode 1", "file": "videos/ep1.mkv"},
    }

    monkeypatch.setattr(
        run_stage,
        "load_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg2://user:pass@localhost:5432/monocorpus",
            database_schema="monocorpus",
        ),
    )
    monkeypatch.setattr(run_stage, "Database", lambda *_args, **_kwargs: fake_db)

    def _fake_run(command, *, cwd):  # noqa: ANN001
        _ = cwd
        status_file = Path(command[command.index("--status-file") + 1])
        status_file.write_text("{}", encoding="utf-8")
        return (0, ["episodes: seen 0 | listed 0 | downloaded 0 | skip-manifest 0 | skip-existing 0 | failed 0 | retries-used 0"])

    monkeypatch.setattr(run_stage, "_run_command_streaming", _fake_run)

    repo = tmp_path / "shayan-video-downloader"
    repo.mkdir(parents=True, exist_ok=True)
    output = tmp_path / "output"
    code = run_stage.main(
        [
            "--stage",
            "download_new",
            "--repo-path",
            str(repo),
            "--output-path",
            str(output),
        ]
    )
    assert code == 1
    assert fake_db.replaced_manifests == []
