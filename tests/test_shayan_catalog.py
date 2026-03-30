"""Tests for Shayan catalog aggregation and re-download logic."""

from __future__ import annotations

from pathlib import Path

from app.modules.shayan.catalog import build_shayan_catalog, request_episode_redownload


class _FakeDb:
    def __init__(self) -> None:
        self.rows = []
        self.manifest_entry = None
        self.latest_snapshot_entries = {}
        self.deleted = []
        self.events = []

    def list_shayan_catalog_rows(self):
        return list(self.rows)

    def get_shayan_manifest_entry(self, entry_key):
        if self.manifest_entry and str(self.manifest_entry.get("entry_key")) == str(entry_key):
            return dict(self.manifest_entry)
        return None

    def get_latest_shayan_snapshot_entries(self):
        return dict(self.latest_snapshot_entries)

    def delete_shayan_manifest_entry(self, entry_key):
        self.deleted.append(str(entry_key))
        return True

    def insert_event(self, event_type, *, task_id=None, run_id=None, panel_id=None, payload=None):
        self.events.append(
            {
                "type": event_type,
                "task_id": task_id,
                "run_id": run_id,
                "panel_id": panel_id,
                "payload": payload or {},
            }
        )


class _FakeRunner:
    def __init__(self) -> None:
        self.calls = []

    def start_task(self, task_id, *, sudo_password=None):  # noqa: ANN001
        self.calls.append({"task_id": str(task_id), "sudo_password": sudo_password})
        return {"action": "captured", "run": None}


def test_build_shayan_catalog_groups_programs_and_flags(tmp_path: Path) -> None:
    output_path = tmp_path / "output"
    local_file = output_path / "videos" / "cartoons" / "Show A" / "S01" / "S01E01.mkv"
    local_file.parent.mkdir(parents=True, exist_ok=True)
    local_file.write_text("video", encoding="utf-8")

    db = _FakeDb()
    db.rows = [
        {
            "entry_key": "a-1",
            "snapshot_payload": {
                "category": "cartoons",
                "program": "Show A",
                "season": 1,
                "episode": 1,
                "title": "Pilot",
                "file": "videos/cartoons/Show A/S01/S01E01.mkv",
            },
            "manifest_payload": {
                "category": "cartoons",
                "program": "Show A",
                "season": 1,
                "episode": 1,
                "title": "Pilot",
                "file": "videos/cartoons/Show A/S01/S01E01.mkv",
            },
            "manifest_payload_hash": "hash-1",
            "yadisk_status": "uploaded",
            "yadisk_remote_path": "/remote/a-1.mkv",
            "yadisk_uploaded_payload_hash": "hash-1",
        },
        {
            "entry_key": "b-1",
            "snapshot_payload": {
                "category": "shows",
                "program": "Show B",
                "season": 2,
                "episode": 3,
                "title": "Episode 3",
                "file": "videos/shows/Show B/S02/S02E03.mkv",
            },
            "manifest_payload": {},
            "manifest_payload_hash": None,
            "yadisk_status": "pending",
            "yadisk_remote_path": None,
            "yadisk_uploaded_payload_hash": None,
        },
    ]

    payload = build_shayan_catalog(db, output_path=output_path)
    assert payload["stats"]["programs"] == 2
    assert payload["stats"]["episodes"] == 2
    assert payload["stats"]["downloaded"] == 1
    assert payload["stats"]["uploaded"] == 1

    show_a = next(item for item in payload["programs"] if item["program"] == "Show A")
    assert show_a["stats"]["downloaded"] == 1
    assert show_a["stats"]["uploaded"] == 1
    episode = show_a["episodes"][0]
    assert episode["entry_key"] == "a-1"
    assert episode["downloaded"] is True
    assert episode["uploaded"] is True


def test_request_episode_redownload_deletes_local_and_manifest(tmp_path: Path) -> None:
    output_path = tmp_path / "output"
    local_file = output_path / "videos" / "cartoons" / "Show A" / "S01" / "S01E01.mkv"
    local_file.parent.mkdir(parents=True, exist_ok=True)
    local_file.write_text("video", encoding="utf-8")

    db = _FakeDb()
    db.manifest_entry = {
        "entry_key": "a-1",
        "payload": {
            "category": "cartoons",
            "program": "Show A",
            "season": 1,
            "episode": 1,
            "file": "videos/cartoons/Show A/S01/S01E01.mkv",
        },
    }
    runner = _FakeRunner()

    result = request_episode_redownload(
        db=db,
        output_path=output_path,
        runner=runner,
        entry_key="a-1",
    )

    assert result["entry_key"] == "a-1"
    assert result["manifest_deleted"] is True
    assert result["local_deleted"] is True
    assert result["download"]["action"] == "captured"
    assert runner.calls[0]["task_id"] == "shayan.download_new"
    assert local_file.exists() is False
    assert db.deleted == ["a-1"]
    assert db.events
    assert db.events[-1]["type"] == "task.artifact"
    assert db.events[-1]["payload"]["kind"] == "shayan.episode_redownload_requested"

