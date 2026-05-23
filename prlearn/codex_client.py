from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .redaction import redact_text


class CodexError(RuntimeError):
    code = "codex_error"


class CodexUnavailable(CodexError):
    code = "unavailable"


class CodexAuthError(CodexError):
    code = "auth"


class CodexTimeout(CodexError):
    code = "timeout"


@dataclass(frozen=True)
class CodexCliClient:
    command: str = "codex"
    timeout: float = 600
    model: str | None = None
    reasoning_effort: str | None = None
    profile: str | None = None
    extra_args: tuple[str, ...] = ()

    @property
    def model_label(self) -> str:
        return self.model or self.profile or "codex-cli-default"

    def version(self) -> dict[str, Any]:
        executable = self._executable()
        try:
            result = subprocess.run([executable, "--version"], text=True, capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodexUnavailable(f"Codex CLI unavailable: {exc}") from exc
        if result.returncode != 0:
            raise CodexUnavailable(clean_error(result))
        return {"version": (result.stdout or result.stderr).strip()}

    def chat(self, *, model: str, messages: list[dict[str, str]], options: dict[str, Any], schema: dict[str, Any] | None = None) -> dict[str, Any]:
        del model, options
        executable = self._executable()
        prompt = render_prompt(messages)
        with tempfile.TemporaryDirectory(prefix="prlearn-codex-") as tmp:
            work_dir = Path(tmp)
            output_path = work_dir / "last-message.txt"
            args = [
                executable,
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--color",
                "never",
                "--output-last-message",
                str(output_path),
            ]
            if schema is not None:
                schema_path = work_dir / "schema.json"
                schema_path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
                args.extend(["--output-schema", str(schema_path)])
            if self.model:
                args.extend(["--model", self.model])
            if self.reasoning_effort:
                args.extend(["-c", f'model_reasoning_effort="{self.reasoning_effort}"'])
            if self.profile:
                args.extend(["--profile", self.profile])
            args.extend(self.extra_args)
            args.append("-")
            try:
                result = subprocess.run(args, input=prompt, text=True, capture_output=True, timeout=self.timeout, check=False, cwd=work_dir)
            except subprocess.TimeoutExpired as exc:
                raise CodexTimeout(f"Codex CLI timed out after {self.timeout:g}s") from exc
            except OSError as exc:
                raise CodexUnavailable(f"Codex CLI unavailable: {exc}") from exc
            if result.returncode != 0:
                message = clean_error(result)
                lower = message.lower()
                if "login" in lower or "auth" in lower or "credential" in lower:
                    raise CodexAuthError(message)
                raise CodexUnavailable(message)
            content = output_path.read_text() if output_path.exists() else ""
            if not content.strip():
                content = result.stdout.strip()
            return {"message": {"content": content}, "model": self.model_label, "provider": "codex-cli"}

    def _executable(self) -> str:
        if os.sep in self.command:
            path = Path(self.command).expanduser()
            if path.exists():
                return str(path)
        found = shutil.which(self.command)
        if not found:
            raise CodexUnavailable(f"Codex CLI command not found: {self.command}")
        return found


def render_prompt(messages: list[dict[str, str]]) -> str:
    parts = []
    for message in messages:
        role = str(message.get("role") or "user").upper()
        content = str(message.get("content") or "")
        parts.append(f"{role}:\n{content}")
    return "\n\n".join(parts)


def clean_error(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stderr or result.stdout or "").strip()
    if not text:
        text = f"Codex CLI exited with status {result.returncode}"
    if "ERROR:" in text:
        text = "ERROR:" + text.rsplit("ERROR:", 1)[-1]
    lines = text.splitlines()
    if len(lines) > 20:
        text = "\n".join(lines[-20:])
    if len(text) > 4000:
        text = text[-4000:]
    return redact_text(text).text


def client_from_config(config: dict[str, Any]) -> CodexCliClient:
    extra_args = config.get("extra_args") or []
    if not isinstance(extra_args, list):
        extra_args = []
    model = os.environ.get("PRLEARN_CODEX_MODEL") or str(config.get("model") or "").strip() or None
    reasoning_effort = os.environ.get("PRLEARN_CODEX_REASONING_EFFORT") or str(config.get("reasoning_effort") or "").strip() or None
    profile = str(config.get("profile") or "").strip() or None
    return CodexCliClient(
        command=str(config.get("command") or "codex"),
        timeout=float(config.get("request_timeout_seconds") or 600),
        model=model,
        reasoning_effort=reasoning_effort,
        profile=profile,
        extra_args=tuple(str(arg) for arg in extra_args),
    )
