from __future__ import annotations

from pathlib import Path

from app.run_log_store import read_run_log, run_log_path


def _write_log(root: Path, task_id: str, run_id: int, count: int) -> Path:
    path = run_log_path(root, task_id, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        (
            "2026-09-02T12:00:00+00:00 | INFO | "
            f"run_id={run_id} task_id={task_id} panel_id=maintenance "
            f"source=stdout | line-{index:02d}\n"
        )
        for index in range(1, count + 1)
    ]
    path.write_text("".join(lines), encoding="utf-8")
    return path


def test_file_log_supports_follow_tail_and_backfill(tmp_path: Path) -> None:
    task_id = "maintenance.example"
    run_id = 42
    _write_log(tmp_path, task_id, run_id, 20)

    all_lines = read_run_log(tmp_path, task_id, run_id, limit=100)
    assert [item["line"] for item in all_lines] == [
        f"line-{index:02d}" for index in range(1, 21)
    ]

    tail = read_run_log(tmp_path, task_id, run_id, tail=True, limit=5)
    assert [item["line"] for item in tail] == [
        f"line-{index:02d}" for index in range(16, 21)
    ]

    before = int(tail[0]["log_id"])
    backfill = read_run_log(
        tmp_path,
        task_id,
        run_id,
        before_log_id=before,
        limit=4,
    )
    assert [item["line"] for item in backfill] == [
        "line-12",
        "line-13",
        "line-14",
        "line-15",
    ]

    after = int(backfill[-1]["log_id"])
    followed = read_run_log(
        tmp_path,
        task_id,
        run_id,
        after_log_id=after,
        limit=2,
    )
    assert [item["line"] for item in followed] == ["line-16", "line-17"]


def test_file_log_missing_or_unterminated_tail_is_safe(tmp_path: Path) -> None:
    assert read_run_log(tmp_path, "missing", 9, tail=True, limit=5) == []

    path = run_log_path(tmp_path, "maintenance.partial", 7)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"2026-09-02T12:00:00+00:00 | INFO | run_id=7 task_id=maintenance.partial "
        b"panel_id=maintenance source=stdout | complete\npartial"
    )

    lines = read_run_log(tmp_path, "maintenance.partial", 7, tail=True, limit=5)
    assert [item["line"] for item in lines] == ["complete"]
