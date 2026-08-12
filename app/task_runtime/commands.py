"""Task command and sudo-policy preparation."""

import shlex
import subprocess
from typing import Any, Dict, Optional


class TaskCommandMixin:
    """Command preflight and transformation behavior for ``TaskRunner``."""

    def _check_sudo_requirements(
        self,
        task: Dict[str, Any],
        *,
        sudo_password: Optional[str],
    ) -> Dict[str, Any]:
        """Detect whether command requires sudo password before task start."""
        command = str((task.get("command") or {}).get("value") or "")
        parsed = self._split_leading_sudo_command(command)
        if not parsed:
            return {"ok": True}

        options, command_tokens = parsed
        probe_tokens, probe_input = self._build_sudo_probe(
            options=options,
            command_tokens=command_tokens,
            sudo_password=sudo_password,
        )
        try:
            result = subprocess.run(
                probe_tokens,
                input=probe_input,
                capture_output=True,
                text=True,
                timeout=12,
            )
        except FileNotFoundError:
            return {
                "ok": False,
                "reason": "sudo_missing",
                "message": "sudo is not installed on this machine.",
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "reason": "sudo_probe_timeout",
                "message": "Timed out while checking sudo access.",
            }

        if int(result.returncode) == 0:
            return {"ok": True}

        output = f"{result.stdout}\n{result.stderr}".strip().lower()
        if self._sudo_password_invalid(output):
            return {
                "ok": False,
                "reason": "sudo_password_invalid",
                "message": "Sudo password is incorrect.",
            }

        if self._sudo_password_required(output):
            if sudo_password:
                return {
                    "ok": False,
                    "reason": "sudo_password_invalid",
                    "message": "Sudo password is incorrect.",
                }
            return {
                "ok": False,
                "reason": "sudo_password_required",
                "message": "Sudo password is required for this command.",
            }

        if self._sudo_access_denied(output):
            return {
                "ok": False,
                "reason": "sudo_access_denied",
                "message": "Current user is not allowed to run this sudo command.",
            }

        return {
            "ok": False,
            "reason": "sudo_unavailable",
            "message": "Unable to validate sudo access for this command.",
        }


    def _prepare_command(
        self,
        command: str,
        sudo_password: Optional[str],
    ) -> tuple[str, Optional[str]]:
        if not sudo_password:
            return command, None

        parsed = self._split_leading_sudo_command(command)
        if not parsed:
            return command, None

        options, command_tokens = parsed
        rebuilt_options: list[str] = []
        i = 0
        saw_stdin_flag = False
        saw_prompt = False
        while i < len(options):
            token = options[i]
            if token == "-n":
                i += 1
                continue
            if token == "-S":
                saw_stdin_flag = True
                rebuilt_options.append(token)
                i += 1
                continue
            if token == "-p":
                saw_prompt = True
                i += 2
                continue
            if token.startswith("--prompt="):
                saw_prompt = True
                i += 1
                continue
            rebuilt_options.append(token)
            i += 1

        if not saw_stdin_flag:
            rebuilt_options.insert(0, "-S")
        if not saw_prompt:
            rebuilt_options[0:0] = ["-p", ""]

        rebuilt = ["sudo", *rebuilt_options, *command_tokens]
        return shlex.join(rebuilt), f"{sudo_password}\n"


    def _build_sudo_probe(
        self,
        *,
        options: list[str],
        command_tokens: list[str],
        sudo_password: Optional[str],
    ) -> tuple[list[str], Optional[str]]:
        sanitized: list[str] = []
        i = 0
        while i < len(options):
            token = options[i]
            if token in {"-n", "-S"}:
                i += 1
                continue
            if token == "-p":
                i += 2
                continue
            if token.startswith("--prompt="):
                i += 1
                continue
            sanitized.append(token)
            i += 1

        # Validate sudo policy for this exact command without executing it.
        probe_suffix = ["-l", "--", *command_tokens]
        if sudo_password:
            return ["sudo", "-S", "-p", "", *sanitized, *probe_suffix], f"{sudo_password}\n"
        return ["sudo", "-n", *sanitized, *probe_suffix], None


    def _split_leading_sudo_command(self, command: str) -> Optional[tuple[list[str], list[str]]]:
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return None
        if not tokens or tokens[0] != "sudo":
            return None

        options: list[str] = []
        i = 1
        while i < len(tokens):
            token = tokens[i]
            if token == "--":
                i += 1
                break
            if not token.startswith("-"):
                break
            options.append(token)
            if token in {"-u", "-g", "-h", "-p", "-r", "-t", "-C", "-T"} and i + 1 < len(tokens):
                options.append(tokens[i + 1])
                i += 2
                continue
            i += 1

        command_tokens = tokens[i:]
        if not command_tokens:
            return None
        return options, command_tokens


    def _sudo_password_required(self, text: str) -> bool:
        return any(
            marker in text
            for marker in (
                "a password is required",
                "password is required",
                "terminal is required",
                "no tty present and no askpass program specified",
            )
        )


    def _sudo_password_invalid(self, text: str) -> bool:
        return any(
            marker in text
            for marker in (
                "incorrect password",
                "sorry, try again",
            )
        )


    def _sudo_access_denied(self, text: str) -> bool:
        return any(
            marker in text
            for marker in (
                "not in the sudoers",
                "is not allowed to execute",
                "is not allowed to run sudo",
            )
        )
