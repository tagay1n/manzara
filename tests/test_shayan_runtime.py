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
        self.upload_candidates: list[dict] = []
        self.uploaded_rows: list[dict] = []
        self.failed_rows: list[dict] = []

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

    def list_shayan_manifest_upload_candidates(self, *, limit=500):  # noqa: ANN001
        _ = limit
        return [dict(item) for item in self.upload_candidates]

    def mark_shayan_manifest_yadisk_uploaded(
        self,
        entry_key,
        *,
        remote_path,
        payload_hash,
    ):  # noqa: ANN001
        self.uploaded_rows.append(
            {
                "entry_key": str(entry_key),
                "remote_path": str(remote_path),
                "payload_hash": str(payload_hash),
            }
        )
        return 1

    def mark_shayan_manifest_yadisk_failed(self, entry_key, *, error_text):  # noqa: ANN001
        self.failed_rows.append(
            {
                "entry_key": str(entry_key),
                "error_text": str(error_text),
            }
        )
        return 1


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


def test_upload_yadisk_stage_uploads_available_files_and_marks_missing(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    artifact_path = tmp_path / "run-artifact.json"
    monkeypatch.setenv("MANZARA_RUN_ARTIFACT_PATH", str(artifact_path))
    fake_db = _FakeDb()
    existing_file = tmp_path / "videos" / "cartoons" / "Show" / "S01" / "S01E01.mkv"
    existing_file.parent.mkdir(parents=True, exist_ok=True)
    existing_file.write_text("video-bytes", encoding="utf-8")
    missing_file = tmp_path / "videos" / "cartoons" / "Show" / "S01" / "S01E02.mkv"

    fake_db.upload_candidates = [
        {
            "entry_key": "ep-1",
            "payload_hash": "hash-1",
            "payload": {"file": str(existing_file), "title": "Episode 1"},
        },
        {
            "entry_key": "ep-2",
            "payload_hash": "hash-2",
            "payload": {"file": str(missing_file), "title": "Episode 2"},
        },
    ]

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
        run_stage,
        "_load_yadisk_upload_settings",
        lambda: run_stage.YaDiskUploadSettings(
            token="oauth-token",
            category_target_dirs={
                "cartoons": "/uploads/cartoons",
                "shows": "/uploads/shows",
            },
        ),
    )

    upload_calls: list[dict] = []

    class _FakeYaDisk:
        def check_token(self):
            return True

        def upload_or_replace(self, local_file, remote_dir, conflict_resolution=0):  # noqa: ANN001
            upload_calls.append(
                {
                    "local_file": str(local_file),
                    "remote_dir": str(remote_dir),
                    "conflict_resolution": conflict_resolution,
                }
            )
            return f"{remote_dir}/S01E01.mkv", "md5"

    monkeypatch.setattr(run_stage, "_create_yadisk_client", lambda _token: _FakeYaDisk())

    repo = tmp_path / "shayan-video-downloader"
    repo.mkdir(parents=True, exist_ok=True)
    output = tmp_path
    code = run_stage.main(
        [
            "--stage",
            "upload_yadisk",
            "--repo-path",
            str(repo),
            "--output-path",
            str(output),
        ]
    )
    assert code == 0
    assert len(upload_calls) == 1
    assert upload_calls[0]["local_file"] == str(existing_file)
    assert upload_calls[0]["remote_dir"].endswith("/uploads/cartoons/Show/S01")
    assert len(fake_db.uploaded_rows) == 1
    assert fake_db.uploaded_rows[0]["entry_key"] == "ep-1"
    assert len(fake_db.failed_rows) == 1
    assert fake_db.failed_rows[0]["entry_key"] == "ep-2"
    assert "local_file_missing" in fake_db.failed_rows[0]["error_text"]

    output_text = capsys.readouterr().out
    assert "shayan upload_yadisk: start considered=2" in output_text
    assert "shayan upload_yadisk: uploading progress=1/2 entry_key=ep-1" in output_text
    assert "shayan upload_yadisk: uploaded progress=1/2 entry_key=ep-1" in output_text
    assert "shayan upload_yadisk: failed progress=2/2 entry_key=ep-2 reason=local_file_missing" in output_text
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["kind"] == "shayan.upload_yadisk_summary"
    assert int(payload["uploaded"]) == 1
    assert int(payload["failed"]) == 1


def test_load_yadisk_upload_settings_reads_new_shayan_targets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
yandex:
  disk:
    oauth_token: token-1
    shayan:
      cartoons: /neurotatarlar/video/shayantv/cartoons
      shows: /neurotatarlar/video/shayantv/shows
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MANZARA_CONFIG_PATH", str(config_path))
    monkeypatch.delenv("SHAYAN_YADISK_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("SHAYAN_YADISK_CARTOONS_TARGET_DIR", raising=False)
    monkeypatch.delenv("SHAYAN_YADISK_SHOWS_TARGET_DIR", raising=False)

    settings = run_stage._load_yadisk_upload_settings()
    assert settings.token == "token-1"
    assert settings.category_target_dirs["cartoons"] == "/neurotatarlar/video/shayantv/cartoons"
    assert settings.category_target_dirs["shows"] == "/neurotatarlar/video/shayantv/shows"
