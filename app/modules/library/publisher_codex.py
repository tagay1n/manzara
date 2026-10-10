"""Subscription-only Codex subprocess and independent best-effort quota telemetry."""

from __future__ import annotations

import json
import math
import os
import queue
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from app.modules.library.publisher_merge_contract import ClusteringWireResponse
from app.runtime_config import (
    config_integer,
    config_number,
    config_text,
    load_runtime_config,
    required_text,
    required_value,
)
from app.task_runtime.logging import redact


@dataclass(frozen=True)
class CodexSettings:
    executable: str
    model: str
    reasoning_effort: str
    scope: str
    web_search: bool
    timeout_seconds: int
    context_window_tokens: int | None

    @classmethod
    def from_config(cls, config=None):
        config = load_runtime_config() if config is None else config
        codex = required_value(config, "codex")
        publisher_merges = required_value(config, "codex", "publisher_merges")
        if not isinstance(codex, dict) or not isinstance(publisher_merges, dict):
            raise ValueError("codex and publisher_merges must be objects")
        if "executable" in publisher_merges:
            raise ValueError("Configure executable only in codex.executable")
        values = dict(publisher_merges)
        values["executable"] = required_text(config, "codex", "executable")
        if set(values) - set(cls.__dataclass_fields__):
            raise ValueError("unsupported publisher_merges configuration")
        for field in cls.__dataclass_fields__:
            if field not in values:
                raise ValueError(f"Missing required config value: codex.publisher_merges.{field}")
        for key in ("model", "executable", "reasoning_effort", "scope"):
            if key in values and (
                not isinstance(values[key], str) or not values[key].strip()
            ):
                raise ValueError(f"{key} must be a non-empty string")
        for key in ("timeout_seconds", "context_window_tokens"):
            if key not in values:
                continue
            value = values[key]
            if key == "context_window_tokens" and value is None:
                continue
            if isinstance(value, str) and value.isascii() and value.isdecimal():
                value = int(value)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{key} must be a positive integer")
            values[key] = value
        if "web_search" in values:
            value = values["web_search"]
            if type(value) is bool:
                pass
            elif isinstance(value, str) and value.lower() in {"true", "false"}:
                values["web_search"] = value.lower() == "true"
            else:
                raise ValueError("web_search must be true or false")
        settings = cls(**values)
        if settings.scope not in {"auto", "new", "all"}:
            raise ValueError("scope must be auto, new or all")
        if settings.reasoning_effort not in {
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
            "ultra",
        }:
            raise ValueError("unsupported reasoning_effort")
        return settings


def isolated_environment(workspace):
    """Only CLI auth and model metadata enter the isolated home; never user config."""
    home = workspace / "codex-home"
    home.mkdir(mode=0o700, exist_ok=True)
    source = Path(config_text("codex", "home")).expanduser()
    try:
        for name in ("auth.json", "models_cache.json"):
            path = source / name
            if path.is_file():
                shutil.copyfile(path, home / name)
                (home / name).chmod(0o600)
    except BaseException:
        shutil.rmtree(home, ignore_errors=True)
        raise
    return {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    } | {"HOME": str(workspace), "CODEX_HOME": str(home)}


def stop_process(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=config_integer("codex", "transport", "terminate_timeout_seconds"))
        return
    try:
        process.wait(timeout=config_integer("codex", "transport", "terminate_timeout_seconds"))
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=config_integer("codex", "transport", "terminate_timeout_seconds"))


def estimate_prompt_tokens(prompt, workspace):
    """Keep tokenizer caching local without mutating the CLI process environment."""
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
    env["TIKTOKEN_CACHE_DIR"] = str(Path(workspace) / "tokenizer-cache")
    try:
        result = subprocess.run([
            sys.executable, "-c",
            "import sys,tiktoken; print(len(tiktoken.get_encoding('o200k_base').encode(sys.stdin.read(), disallowed_special=())))",
        ], input=prompt, capture_output=True, text=True, encoding="utf-8", env=env, timeout=config_integer("codex", "transport", "tokenizer_timeout_seconds"), check=True)
        return math.ceil(int(result.stdout.strip()) * config_number("codex", "transport", "token_estimate_multiplier", minimum=1))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            "Local tokenizer unavailable; initialize the o200k_base tokenizer cache before analysis."
        ) from exc


class _AnalysisOutput:
    """Bounded stream decoding independent of subprocess lifecycle management."""

    def __init__(self, log):
        self.log = log
        self.buffers = {"stdout": b"", "stderr": b""}
        self.stderr_tail = deque(maxlen=config_integer("codex", "transport", "diagnostic_lines"))
        self.terminal = None
        self.reported_model = None

    def feed(self, channel, chunk):
        buffered = self.buffers[channel] + chunk
        if not chunk and buffered:
            buffered += b"\n"
        lines = buffered.split(b"\n")
        self.buffers[channel] = lines.pop()
        for raw in lines:
            if channel == "stdout" and len(raw) > config_integer("codex", "transport", "max_event_bytes"):
                raise ValueError("Codex lifecycle event exceeds the configured byte bound")
            self._line(channel, raw.decode("utf-8", errors="replace"))
        if channel == "stderr" and len(self.buffers[channel]) > config_integer("codex", "transport", "stderr_buffer_bytes"):
            self._diagnostic(self.buffers[channel][-config_integer("codex", "transport", "diagnostic_chars"):].decode("utf-8", errors="replace"))
            self.buffers[channel] = b""
        elif len(self.buffers[channel]) > config_integer("codex", "transport", "max_event_bytes"):
            raise ValueError("Codex lifecycle event exceeds the configured byte bound")

    def _diagnostic(self, line):
        if line.strip():
            safe = redact(line[:config_integer("codex", "transport", "diagnostic_chars")])
            self.stderr_tail.append(safe)
            self.log("Codex diagnostic: " + safe)

    def _line(self, channel, line):
        if channel == "stderr":
            self._diagnostic(line)
            return
        if not line.strip():
            return
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError("Codex lifecycle event must be a JSON object")
        event_type = event.get("type")
        if event_type in {"turn.completed", "turn.failed", "error"}:
            self.terminal = event
        if event_type in {"thread.started", "turn.started", "turn.completed", "turn.failed", "error"}:
            self.log(f"Codex lifecycle: {event_type}")
        if self.reported_model is None and isinstance(event.get("model"), str):
            self.reported_model = event["model"]


class CodexAdapter:
    def __init__(self, settings, workspace):
        self.settings = settings
        self.workspace = Path(workspace).resolve()
        self.env = isolated_environment(self.workspace)
        self.version = None

    def prepare(self, prompt):
        try:
            result = subprocess.run(
                [self.settings.executable, "--version"],
                env=self.env,
                capture_output=True,
                text=True,
                timeout=config_integer("codex", "transport", "preflight_timeout_seconds"),
                check=True,
            )
            self.version = result.stdout.strip()
            help_result = subprocess.run(
                [self.settings.executable, "exec", "--help"],
                env=self.env,
                capture_output=True,
                text=True,
                timeout=config_integer("codex", "transport", "preflight_timeout_seconds"),
                check=True,
            )
            for option in (
                "--ignore-user-config",
                "--ignore-rules",
                "--output-schema",
                "--ephemeral",
                "--json",
            ):
                if option not in help_result.stdout:
                    raise RuntimeError(
                        f"Installed Codex lacks {option}; update the CLI."
                    )
            features = subprocess.run(
                [self.settings.executable, "features", "list"],
                env=self.env,
                capture_output=True,
                text=True,
                timeout=config_integer("codex", "transport", "preflight_timeout_seconds"),
                check=True,
            )
            required = {
                "shell_tool",
                "unified_exec",
                "apps",
                "plugins",
                "multi_agent",
                "view_image",
                "shell_snapshot",
                "skill_search",
                "skip_host_skill_discovery",
            }
            available = {
                line.split()[0] for line in features.stdout.splitlines() if line.split()
            }
            if not required.issubset(available):
                raise RuntimeError(
                    "Installed Codex lacks required isolation feature controls; update the CLI."
                )
            auth = subprocess.run(
                [self.settings.executable, "login", "status"],
                env=self.env,
                capture_output=True,
                text=True,
                timeout=config_integer("codex", "transport", "preflight_timeout_seconds"),
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                "Codex preflight failed; verify executable and CLI installation. No inference or fallback was attempted."
            ) from exc
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Codex CLI is missing. Install it or configure codex.executable."
            ) from exc
        if (
            auth.returncode
            or "Logged in using ChatGPT" not in auth.stdout + auth.stderr
        ):
            raise RuntimeError(
                "ChatGPT subscription authentication is required. Run codex login with the task service account; API-key billing is not supported."
            )
        capacity = self.settings.context_window_tokens
        cache = Path(self.env["CODEX_HOME"]) / "models_cache.json"
        if capacity is None and cache.exists():
            data = json.loads(cache.read_text())
            capacity = next(
                (
                    item.get("context_window")
                    for item in data.get("models", [])
                    if item.get("slug") == self.settings.model
                ),
                None,
            )
        if type(capacity) is not int or capacity <= 0:
            raise RuntimeError(
                "Model context capacity is unknown. Refresh Codex model metadata or set publisher_merges.context_window_tokens to a verified capacity."
            )
        # Reserve includes research, reasoning, output and CLI instruction overhead.
        estimate = estimate_prompt_tokens(prompt, self.workspace)
        reserve = max(config_integer("codex", "transport", "minimum_reserved_tokens"), capacity // config_integer("codex", "transport", "reserve_capacity_divisor"))
        if estimate + reserve > capacity:
            raise RuntimeError(
                f"Complete inventory exceeds context budget: local reference-tokenizer estimate {estimate}, reserve {reserve}, capacity {capacity}. Choose a model with sufficient capacity; inventory will not be truncated."
            )
        return {
            "local_input_token_estimate": estimate,
            "context_capacity": capacity,
            "reserved_tokens": reserve,
            "local_estimate_method": f"o200k_base tokens times {config_number("codex", "transport", "token_estimate_multiplier", minimum=1)}; not service-reported usage",
        }

    def analyze(self, prompt, should_stop, *, log):
        # Keep the CLI environment context stable across run-specific workspaces.
        # Artifacts and isolated credentials remain in each dedicated workspace.
        context = self.workspace.parent / "codex-context"
        context.mkdir(mode=0o700, exist_ok=True)
        schema = self.workspace / "response-schema.json"
        output = self.workspace / "response.json"
        schema.write_text(json.dumps(ClusteringWireResponse.model_json_schema()), encoding="utf-8")
        output.unlink(missing_ok=True)
        command = [
            self.settings.executable,
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--skip-git-repo-check",
            "--json",
            "--sandbox",
            "read-only",
            "--model",
            self.settings.model,
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(output),
            "-C",
            str(context),
        ]
        overrides = {
            "model_provider": "openai",
            "forced_login_method": "chatgpt",
            "model_reasoning_effort": self.settings.reasoning_effort,
            "web_search": "live" if self.settings.web_search else "disabled",
            "features.shell_tool": False,
            "features.unified_exec": False,
            "features.apps": False,
            "features.plugins": False,
            "features.multi_agent": False,
            "features.view_image": False,
            "features.shell_snapshot": False,
            "features.skill_search": False,
            "features.skip_host_skill_discovery": True,
            "project_doc_max_bytes": 0,
            "approval_policy": "never",
        }
        for key, value in overrides.items():
            command.extend(["-c", f"{key}={json.dumps(value)}"])
        command.append("-")
        prompt_file = self.workspace / "prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        terminal, reported_model = self._capture(command, context, prompt_file, should_stop, log)
        if terminal is None or terminal.get("type") != "turn.completed" or not output.exists():
            raise RuntimeError(
                f"Codex analysis failed for configured model {self.settings.model}. Inspect diagnostics.json in the publisher workspace; no fallback was attempted."
            )
        usage = terminal.get("usage")
        return json.loads(output.read_text(encoding="utf-8")), {
            "reported_token_usage": usage if isinstance(usage, dict) else None,
            "reported_model": reported_model,
        }

    def _capture(self, command, context, prompt_file, should_stop, log):
        """Drain both pipes with bounded buffers; retain JSON, never a .log file."""
        started = time.monotonic()
        diagnostics = {"stderr_tail": [], "cancelled": False, "timed_out": False}
        process = None
        with prompt_file.open("rb") as stdin, selectors.DefaultSelector() as selector:
            capture = _AnalysisOutput(log)
            try:
                process = subprocess.Popen(command, cwd=context, env=self.env, stdin=stdin,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                for channel in capture.buffers:
                    stream = getattr(process, channel)
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, channel)
                while selector.get_map() or process.poll() is None:
                    if should_stop():
                        diagnostics["cancelled"] = True
                        raise InterruptedError("Publisher analysis cancelled; no incomplete proposals were imported.")
                    if time.monotonic() - started > self.settings.timeout_seconds:
                        diagnostics["timed_out"] = True
                        raise TimeoutError("Publisher analysis timed out. Increase timeout_seconds or retry manually.")
                    for key, _mask in selector.select(timeout=config_number("codex", "transport", "poll_seconds", minimum=0.001)):
                        channel = key.data
                        chunk = os.read(key.fileobj.fileno(), config_integer("codex", "transport", "read_chunk_bytes"))
                        if not chunk:
                            selector.unregister(key.fileobj)
                        capture.feed(channel, chunk)
                process.wait()
                if process.returncode:
                    raise RuntimeError(f"Codex exited with status {process.returncode}; inspect diagnostics.json in the publisher workspace.")
            except BaseException as exc:
                diagnostics["stream_error"] = redact(exc)
                raise
            finally:
                if process is not None:
                    stop_process(process)
                    diagnostics["returncode"] = process.returncode
                    process.stdout.close()
                    process.stderr.close()
                diagnostics["stderr_tail"] = list(capture.stderr_tail)
                diagnostics["duration_seconds"] = time.monotonic() - started
                (self.workspace / "diagnostics.json").write_text(
                    json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
        return capture.terminal, capture.reported_model

    def close(self):
        home = Path(self.env["CODEX_HOME"])
        if home.exists():
            shutil.rmtree(home)

    def telemetry(self):
        """Version-dependent RPC is isolated and never determines analysis validity."""
        process = None
        try:
            process = subprocess.Popen(
                [self.settings.executable, "app-server"],
                cwd=self.workspace,
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
            lines = queue.Queue(maxsize=config_integer("codex", "transport", "telemetry_queue_entries"))

            def read():
                while line := process.stdout.readline(config_integer("codex", "transport", "read_chunk_bytes")):
                    if not line.endswith("\n"):
                        return
                    try:
                        lines.put_nowait(line)
                    except queue.Full:
                        # Telemetry is optional; never block analysis shutdown on it.
                        return

            threading.Thread(target=read, daemon=True).start()

            def request(identifier, method, params):
                process.stdin.write(
                    json.dumps({"id": identifier, "method": method, "params": params})
                    + "\n"
                )
                process.stdin.flush()
                deadline = time.monotonic() + config_integer("codex", "transport", "telemetry_timeout_seconds")
                while time.monotonic() < deadline:
                    response = json.loads(
                        lines.get(timeout=max(0.01, deadline - time.monotonic()))
                    )
                    if not isinstance(response, dict):
                        continue
                    if response.get("id") == identifier:
                        if "error" in response:
                            raise RuntimeError("telemetry RPC unavailable")
                        return response["result"]
                raise TimeoutError("telemetry timeout")

            request(
                1,
                "initialize",
                {"clientInfo": {"name": "manzara_publisher_merges", "version": "1"}},
            )
            process.stdin.write('{"method":"initialized","params":{}}\n')
            process.stdin.flush()
            return request(2, "account/rateLimits/read", {})
        except (OSError, ValueError, KeyError, RuntimeError, queue.Empty, TimeoutError):
            return None
        finally:
            if process is not None:
                stop_process(process)
                process.stdin.close()
                process.stdout.close()


def quota_observations(before, after):
    def windows(payload):
        if not isinstance(payload, dict):
            return {}
        buckets = payload.get("rateLimitsByLimitId")
        if not isinstance(buckets, dict):
            bucket = payload.get("rateLimits")
            buckets = (
                {bucket.get("limitId", "unknown"): bucket}
                if isinstance(bucket, dict)
                else {}
            )
        result = {}
        for bucket_id, bucket in buckets.items():
            bucket_id = str(bucket_id or "unknown")
            if not isinstance(bucket, dict):
                continue
            for key in ("primary", "secondary"):
                value = bucket.get(key)
                if not isinstance(value, dict):
                    continue
                duration = value.get("windowDurationMins")
                percent = value.get("usedPercent")
                reset = value.get("resetsAt")
                if (
                    type(duration) is not int
                    or duration <= 0
                    or type(percent) not in (int, float)
                    or type(reset) is not int
                    or not math.isfinite(percent)
                    or percent < 0
                ):
                    continue
                result[(bucket_id, duration)] = {
                    "bucket": bucket_id,
                    "duration_minutes": duration,
                    "label": {300: "Five-hour", 10080: "Weekly"}.get(
                        duration, f"{duration}-minute"
                    ),
                    "used_percent": percent,
                    "resets_at": reset,
                }
        return result

    first, last = windows(before), windows(after)
    observations = []
    for key in sorted(first.keys() | last.keys()):
        start, end = first.get(key), last.get(key)
        comparable = bool(
            start
            and end
            and start["resets_at"] == end["resets_at"]
            and end["used_percent"] >= start["used_percent"]
        )
        observations.append(
            {
                **(end or start),
                "before_percent": start["used_percent"] if start else None,
                "after_percent": end["used_percent"] if end else None,
                "delta_percentage_points": end["used_percent"] - start["used_percent"]
                if comparable
                else None,
                "comparison": "comparable; reported precision"
                if comparable
                else "unavailable or reset/non-comparable",
            }
        )
    return {
        "label": "account usage observed during this run",
        "available": bool(observations),
        "before_available": before is not None,
        "after_available": after is not None,
        "note": "Concurrent Codex activity can contribute; percentages are reported account observations, not exact task attribution.",
        "windows": observations,
    }
