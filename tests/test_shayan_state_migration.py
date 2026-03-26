"""Tests for legacy Shayan state migration."""

from __future__ import annotations

import json
from pathlib import Path

from app.modules.shayan.state_migration import migrate_legacy_shayan_state_if_needed


class _FakeDb:
    def __init__(self, *, manifest_present: bool, snapshot_present: bool) -> None:
        self._manifest_present = manifest_present
        self._snapshot_present = snapshot_present
        self.replaced_manifest = None
        self.created_snapshot = None

    def shayan_manifest_entry_count(self) -> int:
        return 1 if self._manifest_present else 0

    def get_latest_shayan_snapshot(self):
        return {"snapshot_id": 1} if self._snapshot_present else None

    def replace_shayan_manifest_entries(self, entries):
        self.replaced_manifest = dict(entries)
        return len(self.replaced_manifest)

    def create_shayan_snapshot(self, entries, *, source=None, generated_at=None):  # noqa: ANN001
        self.created_snapshot = {
            "entries": dict(entries),
            "source": source,
            "generated_at": generated_at,
        }
        return 42


def test_migration_imports_only_missing_parts(tmp_path: Path) -> None:
    artifacts = tmp_path / ".manzara" / "shayan"
    (artifacts / "snapshots").mkdir(parents=True, exist_ok=True)
    (artifacts / "status.json").write_text(
        json.dumps({"ep-1": {"title": "Episode 1"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifacts / "snapshots" / "latest.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-03-26T10:00:00+00:00",
                "source": "https://tt.shayantv.ru",
                "entries": {"ep-2": {"title": "Episode 2"}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    db = _FakeDb(manifest_present=True, snapshot_present=False)
    result = migrate_legacy_shayan_state_if_needed(db, artifacts_dir=artifacts)
    assert result["migrated"] is True
    assert db.replaced_manifest is None
    assert db.created_snapshot is not None
    assert result["snapshot_id"] == 42
