"""Subscription-only Codex subprocess and independent best-effort quota telemetry."""

from __future__ import annotations

import json
import math
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.modules.library.publisher_merge_contract import ClusteringWireResponse
from app.runtime_config import load_runtime_config


@dataclass(frozen=True)
class CodexSettings:
    executable: str = "codex"
    model: str = "gpt-6.1-sol"
    reasoning_effort: str = "medium"
    scope: str = "auto"
    web_search: bool = True
    timeout_seconds: int = 3600
    context_window_tokens: int | None = None

    @classmethod
    def from_config(cls, config=None):
        config = load_runtime_config() if config is None else config
        codex = config.get("codex", {})
        if not isinstance(codex, dict) or not isinstance(
            codex.get("publisher_merges", {}), dict
        ):
            raise ValueError("codex and publisher_merges must be objects")
        values = dict(codex.get("publisher_merges", {}))
        values["executable"] = codex.get("executable", "codex")
        if set(values) - set(cls.__dataclass_fields__):
            raise ValueError("unsupported publisher_merges configuration")
        for key in ("model", "executable", "reasoning_effort", "scope"):
            if key in values and (
                not isinstance(values[key], str) or not values[key].strip()
            ):
                raise ValueError(f"{key} must be a non-empty string")
        for key in ("timeout_seconds", "context_window_tokens"):
            if key not in values:
                continue
            value = values[key]
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
    source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    for name in ("auth.json", "models_cache.json"):
        path = source / name
        if path.is_file():
            shutil.copyfile(path, home / name)
            (home / name).chmod(0o600)
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
        process.wait(timeout=3)
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)


def estimate_prompt_tokens(prompt, workspace):
    # The reference encoding is an estimate; configured models may tokenize differently.
    # Keep downloaded tokenizer data in the configured artifact workspace.
    import tiktoken

    previous = os.environ.get("TIKTOKEN_CACHE_DIR")
    os.environ["TIKTOKEN_CACHE_DIR"] = str(Path(workspace) / "tokenizer-cache")
    try:
        encoding = tiktoken.get_encoding("o200k_base")
        return math.ceil(len(encoding.encode(prompt, disallowed_special=())) * 1.15)
    except (OSError, ValueError, RuntimeError) as exc:
        raise RuntimeError(
            "Local tokenizer unavailable; initialize the o200k_base tokenizer cache before analysis."
        ) from exc
    finally:
        if previous is None:
            os.environ.pop("TIKTOKEN_CACHE_DIR", None)
        else:
            os.environ["TIKTOKEN_CACHE_DIR"] = previous


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
                timeout=15,
                check=True,
            )
            self.version = result.stdout.strip()
            help_result = subprocess.run(
                [self.settings.executable, "exec", "--help"],
                env=self.env,
                capture_output=True,
                text=True,
                timeout=15,
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
                timeout=15,
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
                timeout=15,
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
        reserve = max(32000, capacity // 3)
        if estimate + reserve > capacity:
            raise RuntimeError(
                f"Complete inventory exceeds context budget: local reference-tokenizer estimate {estimate}, reserve {reserve}, capacity {capacity}. Choose a model with sufficient capacity; inventory will not be truncated."
            )
        return {
            "local_input_token_estimate": estimate,
            "context_capacity": capacity,
            "reserved_tokens": reserve,
            "local_estimate_method": "o200k_base tokens plus 15% margin; not service-reported usage",
        }

    def analyze(self, prompt, should_stop):
        # Keep the CLI environment context stable across run-specific workspaces.
        # Artifacts and isolated credentials remain in each dedicated workspace.
        context = self.workspace.parent / "codex-context"
        context.mkdir(mode=0o700, exist_ok=True)
        schema = self.workspace / "response-schema.json"
        output = self.workspace / "response.json"
        schema.write_text(json.dumps(ClusteringWireResponse.model_json_schema()))
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
        prompt_file.write_text(prompt)
        started = time.monotonic()
        with (
            prompt_file.open("rb") as stdin,
            (self.workspace / "lifecycle.jsonl").open("wb") as stdout,
            (self.workspace / "cli-stderr.log").open("wb") as stderr,
        ):
            process = subprocess.Popen(
                command,
                cwd=context,
                env=self.env,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            try:
                while process.poll() is None:
                    if should_stop():
                        raise InterruptedError(
                            "Publisher analysis cancelled; no incomplete proposals were imported."
                        )
                    if time.monotonic() - started > self.settings.timeout_seconds:
                        raise TimeoutError(
                            "Publisher analysis timed out. Increase timeout_seconds or retry manually."
                        )
                    time.sleep(0.1)
            finally:
                stop_process(process)
        events = []
        for line in (self.workspace / "lifecycle.jsonl").read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        completed = [event for event in events if event.get("type") == "turn.completed"]
        if process.returncode or not completed or not output.exists():
            raise RuntimeError(
                f"Codex analysis failed for configured model {self.settings.model}. Check account model availability and the dedicated CLI artifact log; no fallback was attempted."
            )
        usage = completed[-1].get("usage")
        return json.loads(output.read_text()), {
            "reported_token_usage": usage if isinstance(usage, dict) else None,
            "reported_model": next(
                (event.get("model") for event in events if event.get("model")), None
            ),
        }

    def close(self):
        shutil.rmtree(self.env["CODEX_HOME"], ignore_errors=True)

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
            lines = queue.Queue()

            def read():
                for line in process.stdout:
                    lines.put(line)

            threading.Thread(target=read, daemon=True).start()

            def request(identifier, method, params):
                process.stdin.write(
                    json.dumps({"id": identifier, "method": method, "params": params})
                    + "\n"
                )
                process.stdin.flush()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    response = json.loads(
                        lines.get(timeout=max(0.01, deadline - time.monotonic()))
                    )
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
