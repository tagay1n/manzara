"""API/runtime behavior tests for Manzara."""

from __future__ import annotations

import asyncio
import json
import re
import time

import app.tasks as task_runtime


def _wait_for_status(main_app, run_id: int, expected: set[str], timeout_seconds: float = 4.0):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        run = main_app.state.db.get_run(run_id)
        if run and run["status"] in expected:
            return run
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not reach expected status: {expected}")


def test_toggle_task_reports_sudo_password_required(test_client, monkeypatch) -> None:
    client, main_app = test_client

    def _always_require(_task, *, sudo_password=None):
        _ = sudo_password
        return {
            "ok": False,
            "reason": "sudo_password_required",
            "message": "Sudo password is required for this command.",
        }

    monkeypatch.setattr(main_app.state.runner, "_check_sudo_requirements", _always_require)
    response = client.post("/api/tasks/maintenance.pgbackrest_backup_full/toggle")
    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "sudo_password_required"
    assert payload["reason"] == "sudo_password_required"


def test_sudo_preflight_checks_exact_command_policy(test_client, monkeypatch) -> None:
    client, _main_app = test_client
    captured = {}

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "sudo: a password is required"

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = dict(kwargs)
        return _Result()

    monkeypatch.setattr(task_runtime.subprocess, "run", _fake_run)

    response = client.post("/api/tasks/maintenance.pgbackrest_backup_incr/toggle")
    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "sudo_password_required"

    probe_cmd = captured["cmd"]
    assert "-l" in probe_cmd
    assert "--" in probe_cmd
    assert any("pgbackrest" in token for token in probe_cmd)


def test_toggle_task_start_and_complete(test_client, wait_for_terminal_run) -> None:
    client, main_app = test_client

    response = client.post("/api/tasks/maintenance.quick/toggle")
    assert response.status_code == 200
    run_id = int(response.json()["run"]["run_id"])

    run = wait_for_terminal_run(main_app, run_id)
    assert run["status"] == "completed"

    logs = client.get(f"/api/runs/{run_id}/logs").json()["lines"]
    assert any("quick-ok" in line["line"] for line in logs)


def test_pgbackrest_backup_emits_preflight_and_start_logs(
    test_client,
    wait_for_terminal_run,
    monkeypatch,
) -> None:
    client, main_app = test_client
    main_app.state.db.seed_tasks(
        [
            {
                "task_id": "maintenance.pgbackrest_backup_full",
                "panel_id": "backup",
                "title": "Full backup",
                "task_type": "backup",
                "icon_idle": "Database",
                "icon_running": "Square",
                "cwd": ".",
                "command": {"mode": "shell", "value": "printf 'pgBackRest progress\\n'"},
            }
        ]
    )
    monkeypatch.setattr(
        main_app.state.runner,
        "_capture_pgbackrest_s3_state",
        lambda **_: {"ok": True, "bucket": "ttbackups", "label_count": 2},
    )
    monkeypatch.setattr(
        task_runtime,
        "wait_for_pgbackrest_s3_change",
        lambda **_: {"ok": True, "labels_added": ["20260829-010101F"]},
    )

    response = client.post("/api/tasks/maintenance.pgbackrest_backup_full/toggle")
    assert response.status_code == 200
    run = wait_for_terminal_run(main_app, int(response.json()["run"]["run_id"]))
    assert run["status"] == "completed"

    logs = client.get(f"/api/runs/{run['run_id']}/logs").json()["lines"]
    messages = [str(item["line"]) for item in logs]
    assert any("Preparing full pgBackRest backup" in message for message in messages)
    assert any("Starting full pgBackRest backup" in message for message in messages)
    assert "pgBackRest progress" in messages


def test_task_artifact_event_and_summary_without_log_parsing(
    test_client,
    wait_for_terminal_run,
) -> None:
    client, main_app = test_client

    main_app.state.db.seed_tasks(
        [
            {
                "task_id": "maintenance.artifact_file_emit",
                "panel_id": "maintenance",
                "title": "artifact file emit",
                "task_type": "test",
                "icon_idle": "Play",
                "icon_running": "Square",
                "cwd": ".",
                "command": {
                    "mode": "shell",
                    "value": (
                        "python3 -c \"import json,os,pathlib; "
                        "p=pathlib.Path(os.environ['MANZARA_RUN_ARTIFACT_PATH']); "
                        "p.parent.mkdir(parents=True,exist_ok=True); "
                        "tmp=p.with_suffix(p.suffix + '.tmp'); "
                        "tmp.write_text(json.dumps({'kind':'test.summary','items_processed':3}),encoding='utf-8'); "
                        "tmp.replace(p); "
                        "print('runtime done')\""
                    ),
                },
            }
        ]
    )

    response = client.post("/api/tasks/maintenance.artifact_file_emit/toggle")
    assert response.status_code == 200
    run_id = int(response.json()["run"]["run_id"])
    run = wait_for_terminal_run(main_app, run_id)
    assert run["status"] == "completed"

    artifacts = None
    deadline = time.time() + 2.0
    while time.time() < deadline:
        run_payload = main_app.state.db.get_run(run_id)
        summary = run_payload.get("summary") if isinstance(run_payload, dict) else {}
        current = summary.get("artifacts") if isinstance(summary, dict) else None
        if isinstance(current, dict) and current.get("kind"):
            artifacts = current
            break
        time.sleep(0.05)

    assert isinstance(artifacts, dict)
    assert artifacts.get("kind") == "test.summary"
    assert int(artifacts.get("items_processed") or 0) == 3

    events = main_app.state.db.get_events_after(0, limit=400)
    artifact_events = [event for event in events if str(event.get("type") or "") == "task.artifact"]
    assert artifact_events
    latest = artifact_events[-1]
    assert int(latest.get("run_id") or 0) == run_id
    payload = latest.get("payload") if isinstance(latest.get("payload"), dict) else {}
    assert payload.get("kind") == "test.summary"


def test_run_logs_support_tail_and_backfill_pagination(test_client) -> None:
    client, main_app = test_client
    task = main_app.state.db.get_task("maintenance.quick")
    assert task is not None
    run_id = main_app.state.db.create_run(task)
    main_app.state.db.mark_run_started(run_id, pid=99999)
    for index in range(1, 21):
        main_app.state.db.append_log(run_id, "stdout", f"line-{index:02d}")
    main_app.state.db.finish_run(run_id, "completed", 0, None)

    all_payload = client.get(f"/api/runs/{run_id}/logs?limit=2000")
    assert all_payload.status_code == 200
    all_lines = all_payload.json()["lines"]
    assert len(all_lines) >= 20
    all_ids = [int(item["log_id"]) for item in all_lines]

    tail_payload = client.get(f"/api/runs/{run_id}/logs?tail=true&limit=5")
    assert tail_payload.status_code == 200
    tail = tail_payload.json()
    tail_ids = [int(item["log_id"]) for item in tail["lines"]]
    assert tail_ids == all_ids[-5:]
    assert int(tail["next_after_log_id"]) == all_ids[-1]
    assert int(tail["next_before_log_id"]) == all_ids[-5]
    assert tail["has_more_before"] is True

    backfill_payload = client.get(
        f"/api/runs/{run_id}/logs?before_log_id={tail['next_before_log_id']}&limit=4"
    )
    assert backfill_payload.status_code == 200
    backfill = backfill_payload.json()
    backfill_ids = [int(item["log_id"]) for item in backfill["lines"]]
    assert backfill_ids == all_ids[-9:-5]
    assert int(backfill["next_after_log_id"]) == all_ids[-6]
    assert int(backfill["next_before_log_id"]) == all_ids[-9]
    assert backfill["has_more_before"] is True


def test_run_logs_reject_conflicting_cursor_modes(test_client) -> None:
    client, main_app = test_client
    task = main_app.state.db.get_task("maintenance.quick")
    assert task is not None
    run_id = main_app.state.db.create_run(task)

    conflict = client.get(f"/api/runs/{run_id}/logs?tail=true&after_log_id=10")
    assert conflict.status_code == 400
    assert "cannot be combined" in conflict.json()["detail"]



def test_task_run_writes_artifact_log_with_uniform_format(
    test_client,
    wait_for_terminal_run,
    tmp_path,
) -> None:
    client, main_app = test_client
    artifacts_root = tmp_path / "_artifacts" / "task_runs"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    main_app.state.runner._artifacts_root = artifacts_root

    response = client.post("/api/tasks/maintenance.quick/toggle")
    assert response.status_code == 200
    run_id = int(response.json()["run"]["run_id"])
    run = wait_for_terminal_run(main_app, run_id)
    assert run["status"] == "completed"

    run_log_path = artifacts_root / "maintenance.quick" / f"run-{run_id}.log"
    assert run_log_path.exists()

    lines = run_log_path.read_text(encoding="utf-8").splitlines()
    assert lines
    assert any("source=stdout | quick-ok" in line for line in lines)
    assert any("final status=completed exit_code=0" in line for line in lines)

    # Uniform log schema: timestamp | LEVEL | run/task/panel/source context | message
    assert re.match(
        (
            r"^\d{4}-\d{2}-\d{2}T.*\|\s+[A-Z]+\s+\|\s+"
            r"run_id=\d+\s+task_id=[^\s]+\s+panel_id=[^\s]+\s+source=[^\s]+\s+\|\s+.+$"
        ),
        lines[0],
    )


def test_task_run_artifact_log_captures_startup_exception(
    test_client,
    wait_for_terminal_run,
    tmp_path,
    monkeypatch,
) -> None:
    client, main_app = test_client
    artifacts_root = tmp_path / "_artifacts" / "task_runs"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    main_app.state.runner._artifacts_root = artifacts_root

    def _boom(*_args, **_kwargs):
        raise RuntimeError("popen-boom")

    monkeypatch.setattr(task_runtime.subprocess, "Popen", _boom)

    response = client.post("/api/tasks/maintenance.quick/toggle")
    assert response.status_code == 200
    run_id = int(response.json()["run"]["run_id"])
    run = wait_for_terminal_run(main_app, run_id)
    assert run["status"] == "failed"
    assert "popen-boom" in str(run.get("error_text") or "")

    run_log_path = artifacts_root / "maintenance.quick" / f"run-{run_id}.log"
    assert run_log_path.exists()
    log_text = run_log_path.read_text(encoding="utf-8")
    assert "source=runtime | exception=popen-boom" in log_text


def test_task_logs_are_redacted_in_db_and_artifact_files(
    test_client,
    wait_for_terminal_run,
    tmp_path,
) -> None:
    client, main_app = test_client
    artifacts_root = tmp_path / "_artifacts" / "task_runs"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    main_app.state.runner._artifacts_root = artifacts_root

    main_app.state.db.seed_tasks(
        [
            {
                "task_id": "maintenance.secret_log_redaction",
                "panel_id": "maintenance",
                "title": "Secret log redaction",
                "task_type": "backup",
                "icon_idle": "Play",
                "icon_running": "Square",
                "cwd": ".",
                "command": {
                    "mode": "shell",
                    "value": (
                        "python3 -c \"print('token=abc123 "
                        "aws_secret_access_key=SECRETVALUE "
                        "--repo1-s3-key-secret=SECRETKEY "
                        "--repo1-s3-key=ACCESSKEY "
                        "Authorization: Bearer VERYSECRETTOKEN "
                        "https://example.com/path?token=QUERYTOKEN&x=1 "
                        "https://user:plainpass@example.com/path')\""
                    ),
                },
            }
        ]
    )

    response = client.post("/api/tasks/maintenance.secret_log_redaction/toggle")
    assert response.status_code == 200
    run_id = int(response.json()["run"]["run_id"])
    run = wait_for_terminal_run(main_app, run_id)
    assert run["status"] == "completed"

    logs = client.get(f"/api/runs/{run_id}/logs").json()["lines"]
    combined = "\n".join(str(line.get("line") or "") for line in logs)
    assert "<redacted>" in combined
    assert "abc123" not in combined
    assert "SECRETVALUE" not in combined
    assert "SECRETKEY" not in combined
    assert "ACCESSKEY" not in combined
    assert "VERYSECRETTOKEN" not in combined
    assert "QUERYTOKEN" not in combined
    assert "plainpass" not in combined

    run_log_path = artifacts_root / "maintenance.secret_log_redaction" / f"run-{run_id}.log"
    assert run_log_path.exists()
    artifact_text = run_log_path.read_text(encoding="utf-8")
    assert "<redacted>" in artifact_text
    assert "abc123" not in artifact_text
    assert "SECRETVALUE" not in artifact_text
    assert "SECRETKEY" not in artifact_text
    assert "ACCESSKEY" not in artifact_text
    assert "VERYSECRETTOKEN" not in artifact_text
    assert "QUERYTOKEN" not in artifact_text
    assert "plainpass" not in artifact_text


def test_stream_stdout_failures_emit_actionable_log_line(test_client) -> None:
    _client, main_app = test_client
    runner = main_app.state.runner
    task = main_app.state.db.get_task("maintenance.quick")
    assert task is not None
    run_id = main_app.state.db.create_run(task)

    class _BoomStream:
        def __init__(self) -> None:
            self._step = 0

        def __iter__(self):
            return self

        def __next__(self) -> str:
            if self._step == 0:
                self._step = 1
                return "line-before-error\n"
            raise RuntimeError("stream exploded")

    class _Proc:
        stdout = _BoomStream()

    runner._stream_stdout_lines(_Proc(), run_id, task["task_id"], task["panel_id"])
    logs = main_app.state.db.get_logs(run_id, after_log_id=0, limit=50)
    combined = "\n".join(str(item.get("line") or "") for item in logs)
    assert "line-before-error" in combined
    assert "log_stream_error=stream exploded" in combined


def test_stream_stdout_closed_file_error_is_ignored(test_client) -> None:
    _client, main_app = test_client
    runner = main_app.state.runner
    task = main_app.state.db.get_task("maintenance.quick")
    assert task is not None
    run_id = main_app.state.db.create_run(task)

    class _ClosedStream:
        def __iter__(self):
            return self

        def __next__(self) -> str:
            raise ValueError("I/O operation on closed file")

    class _Proc:
        stdout = _ClosedStream()

    runner._stream_stdout_lines(_Proc(), run_id, task["task_id"], task["panel_id"])
    logs = main_app.state.db.get_logs(run_id, after_log_id=0, limit=50)
    combined = "\n".join(str(item.get("line") or "") for item in logs)
    assert "log_stream_error=" not in combined


def test_task_completion_not_blocked_by_open_stdout_fd(test_client, wait_for_terminal_run) -> None:
    client, main_app = test_client

    main_app.state.db.seed_tasks(
        [
            {
                "task_id": "maintenance.stdout_fd_open",
                "panel_id": "maintenance",
                "title": "stdout fd open",
                "task_type": "backup",
                "icon_idle": "Play",
                "icon_running": "Square",
                "cwd": ".",
                "command": {
                    "mode": "shell",
                    "value": (
                        "python3 -c \"import subprocess,sys; "
                        "subprocess.Popen(['python3','-c','import time; time.sleep(3)'], "
                        "stdout=sys.stdout, stderr=sys.stderr); "
                        "print('parent-exit', flush=True)\""
                    ),
                },
            }
        ]
    )

    response = client.post("/api/tasks/maintenance.stdout_fd_open/toggle")
    assert response.status_code == 200
    run_id = int(response.json()["run"]["run_id"])
    run = wait_for_terminal_run(main_app, run_id, timeout_seconds=3.0)
    assert run["status"] == "completed"


def test_toggle_task_graceful_then_force(test_client, wait_for_terminal_run) -> None:
    client, main_app = test_client

    started = client.post("/api/tasks/maintenance.ignore_sigint/toggle").json()
    run_id = int(started["run"]["run_id"])
    _wait_for_status(main_app, run_id, {"running"})

    graceful = client.post("/api/tasks/maintenance.ignore_sigint/toggle")
    assert graceful.status_code == 200
    assert graceful.json()["action"] == "stop_graceful"

    force = client.post("/api/tasks/maintenance.ignore_sigint/toggle")
    assert force.status_code == 200
    assert force.json()["action"] in {"stop_force", "noop"}

    run = wait_for_terminal_run(main_app, run_id)
    assert run["status"] == "stopped"


def test_stop_all_two_step_force(test_client, wait_for_terminal_run) -> None:
    client, main_app = test_client

    started = client.post("/api/tasks/maintenance.ignore_sigint/toggle").json()
    run_id = int(started["run"]["run_id"])
    _wait_for_status(main_app, run_id, {"running"})

    first = client.post("/api/system/stop-all")
    assert first.status_code == 200
    assert first.json()["action"] == "stop_all_graceful"

    second = client.post("/api/system/stop-all")
    assert second.status_code == 200
    assert second.json()["action"] in {"stop_all_force", "noop"}

    run = wait_for_terminal_run(main_app, run_id)
    assert run["status"] == "stopped"


def test_events_stream_outputs_sse_frames(test_client) -> None:
    client, main_app = test_client

    event = main_app.state.db.insert_event(
        "task.started",
        task_id="maintenance.quick",
        run_id=1,
        panel_id="maintenance",
        payload={"status": "starting"},
    )

    class _FakeRequest:
        headers = {}

        async def is_disconnected(self) -> bool:
            return False

    async def _read_first_chunk():
        response = await main_app.events_stream(_FakeRequest(), after_event_id=0)
        assert response.media_type == "text/event-stream"
        iterator = response.body_iterator
        chunk = await anext(iterator)
        if hasattr(iterator, "aclose"):
            await iterator.aclose()
        return chunk

    chunk = asyncio.run(_read_first_chunk())
    text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
    assert text.startswith(f"id: {event['event_id']}")
    assert "\nevent: task.started\n" in text
    assert "\ndata: " in text

    payload_line = [line for line in text.splitlines() if line.startswith("data: ")][0]
    payload = json.loads(payload_line.replace("data: ", "", 1))
    assert payload["type"].startswith("task.") or payload["type"].startswith("system.")
