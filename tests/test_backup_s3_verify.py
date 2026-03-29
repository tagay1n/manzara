"""Unit tests for maintenance S3 backup verification helpers."""

from __future__ import annotations

from typing import Any, Dict

from app.modules.maintenance import backup_s3_verify as verify


def test_capture_pgbackrest_s3_state_collects_labels(monkeypatch) -> None:
    monkeypatch.setattr(
        verify,
        "load_s3_credentials",
        lambda **_: {
            "ok": True,
            "aws_access_key_id": "id",
            "aws_secret_access_key": "secret",
            "source": "test",
        },
    )

    class _Session:
        def client(self, **_: Any) -> object:
            return object()

    def _fake_list_child_prefixes(_s3: Any, _bucket: str, prefix: str, *, limit: int = 5000) -> Dict[str, Any]:
        _ = limit
        if prefix.startswith("var/lib/pgbackrest/backup/"):
            return {"labels": ["a1", "a2"], "count": 2, "prefix": prefix}
        return {"labels": ["a2", "a3"], "count": 2, "prefix": prefix}

    monkeypatch.setattr(verify, "Session", _Session)
    monkeypatch.setattr(verify, "list_child_prefixes", _fake_list_child_prefixes)

    state = verify.capture_pgbackrest_s3_state(
        command_value="sudo -n -u postgres pgbackrest --stanza=mono --type=incr backup",
        bucket="bucket",
    )
    assert state["ok"] is True
    assert state["stanza"] == "mono"
    assert state["bucket"] == "bucket"
    assert state["labels"] == ["a1", "a2", "a3"]
    assert state["label_count"] == 3
    assert "a2" in state["label_prefixes"]
    assert len(state["label_prefixes"]["a2"]) == 2


def test_wait_for_pgbackrest_s3_change_detects_new_label(monkeypatch) -> None:
    before = {
        "ok": True,
        "bucket": "bucket",
        "stanza": "mono",
        "endpoint": "https://s3.example.local",
        "labels": ["old-1"],
    }

    monkeypatch.setattr(
        verify,
        "capture_pgbackrest_s3_state",
        lambda **_: {
            "ok": True,
            "bucket": "bucket",
            "stanza": "mono",
            "endpoint": "https://s3.example.local",
            "labels": ["old-1", "new-1"],
            "label_prefixes": {"new-1": ["backup/mono/new-1/"]},
        },
    )
    monkeypatch.setattr(
        verify,
        "_validate_new_labels_have_required_files",
        lambda _state, labels_added, **_: {
            "ok": True,
            "bucket": "bucket",
            "stanza": "mono",
            "label": labels_added[0],
            "prefix": "backup/mono/new-1/",
            "object_count": 4,
        },
    )

    result = verify.wait_for_pgbackrest_s3_change(
        before_state=before,
        wait_seconds=0,
        poll_interval_seconds=0.1,
    )
    assert result["ok"] is True
    assert result["labels_added"] == ["new-1"]
    assert result["label"] == "new-1"


def test_wait_for_pgbackrest_s3_change_fails_when_no_new_labels(monkeypatch) -> None:
    before = {
        "ok": True,
        "bucket": "bucket",
        "stanza": "mono",
        "endpoint": "https://s3.example.local",
        "labels": ["old-1"],
    }

    monkeypatch.setattr(
        verify,
        "capture_pgbackrest_s3_state",
        lambda **_: {
            "ok": True,
            "bucket": "bucket",
            "stanza": "mono",
            "endpoint": "https://s3.example.local",
            "labels": ["old-1"],
            "label_prefixes": {},
        },
    )

    result = verify.wait_for_pgbackrest_s3_change(
        before_state=before,
        wait_seconds=0,
        poll_interval_seconds=0.1,
    )
    assert result["ok"] is False
    assert "No new backup label appeared in S3" in str(result["error"])
