from __future__ import annotations

import os
from pathlib import Path

from . import __version__
from .config import codex_config, github_config, load_config, ollama_config, openai_config
from .github import GitHubClientError, github_app_client_from_config, github_app_configured
from .ollama_client import OllamaError, OllamaRemoteHostBlocked, client_from_config
from .schedule import status as schedule_status
from .util import command_exists, python_version_ok, redact, run_command


def check(home: Path, db_path: Path, *, strict_ollama: bool = False) -> dict[str, object]:
    db_writable = False
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        probe = db_path.parent / ".prlearn-write-test"
        probe.write_text("ok")
        probe.unlink()
        db_writable = True
    except OSError:
        db_writable = False
    gh_exists = command_exists("gh")
    gh_auth = {"ok": False, "message": "gh not found"}
    if gh_exists:
        result = run_command(["gh", "auth", "status"], timeout=15)
        gh_auth = {"ok": result.returncode == 0, "message": redact(result.stderr.strip() or result.stdout.strip())}
    config = load_config(home, db_path)
    github_app = check_github_app(github_config(config))
    ollama = check_ollama(ollama_config(config))
    codex = check_codex(codex_config(config))
    openai = check_openai(openai_config(config))
    return {
        "version": __version__,
        "python": {"ok": python_version_ok()},
        "db_path_writable": db_writable,
        "git_exists": command_exists("git"),
        "gh_exists": gh_exists,
        "gh_auth": gh_auth,
        "github_app": github_app,
        "ollama_required": strict_ollama,
        "ollama": ollama,
        "codex": codex,
        "openai": openai,
        "sqlite_vec": check_sqlite_vec(),
        "scheduler": schedule_status(home),
    }


def overall_ok(report: dict[str, object]) -> bool:
    base_ok = bool(report["python"]["ok"] and report["db_path_writable"] and report["git_exists"])
    if report.get("ollama_required"):
        ollama = report.get("ollama")
        if isinstance(ollama, dict):
            base_ok = base_ok and bool(ollama.get("ok"))
    return base_ok


def check_ollama(config: dict[str, object]) -> dict[str, object]:
    required_models = dict(config.get("models") or {})
    required_unique = sorted({model for model in required_models.values()})
    base_url = str(config.get("base_url") or "http://127.0.0.1:11434")
    report: dict[str, object] = {
        "ok": False,
        "reachable": False,
        "base_url": base_url,
        "localhost_only": bool(config.get("require_localhost", True)),
        "version": None,
        "models": {"required": required_models, "installed": [], "missing": required_unique},
        "suggested_pulls": [f"ollama pull {model}" for model in required_unique],
        "message": "Ollama is optional for heuristic workflows.",
    }
    try:
        client = client_from_config(config, timeout=2)
        version = client.version()
        installed = client.installed_models()
    except OllamaRemoteHostBlocked as exc:
        report["message"] = str(exc)
        return report
    except OllamaError as exc:
        report["message"] = str(exc)
        return report
    missing = sorted({model for model in required_models.values() if model not in installed})
    report.update(
        {
            "ok": not missing,
            "reachable": True,
            "version": version.get("version") if isinstance(version, dict) else None,
            "models": {"required": required_models, "installed": sorted(installed), "missing": missing},
            "suggested_pulls": [f"ollama pull {model}" for model in missing],
            "message": "Ollama reachable." if not missing else "Ollama reachable, but one or more configured models are missing.",
        }
    )
    return report


def check_github_app(config: dict[str, object]) -> dict[str, object]:
    report: dict[str, object] = {
        "ok": False,
        "configured": False,
        "auth_mode": config.get("auth_mode") or "auto",
        "api_url": config.get("api_url") or "https://api.github.com",
        "message": "GitHub App auth is optional. Configure it for stable unattended sync.",
    }
    if not github_app_configured(config):
        return report
    report["configured"] = True
    try:
        client = github_app_client_from_config(config)
        repos = client.installation_repositories()
    except (GitHubClientError, OSError) as exc:
        report["message"] = redact(str(exc))
        return report
    report.update(
        {
            "ok": True,
            "repositories_visible": len(repos),
            "message": "GitHub App installation auth works.",
        }
    )
    return report


def check_codex(config: dict[str, object]) -> dict[str, object]:
    command = str(config.get("command") or "codex")
    exists = command_exists(command)
    report: dict[str, object] = {
        "ok": False,
        "command": command,
        "exists": exists,
        "login_status": None,
        "message": "Codex is optional. Use `codex login` to enable the codex extraction engine.",
    }
    if not exists:
        return report
    version = run_command([command, "--version"], timeout=15)
    status = run_command([command, "login", "status"], timeout=15)
    report.update(
        {
            "ok": status.returncode == 0,
            "version": redact((version.stdout or version.stderr).strip()),
            "login_status": redact((status.stdout or status.stderr).strip()),
            "message": "Codex CLI logged in." if status.returncode == 0 else "Codex CLI found but not logged in. Run `codex login`.",
        }
    )
    return report


def check_openai(config: dict[str, object]) -> dict[str, object]:
    env_name = str(config.get("api_key_env") or "OPENAI_API_KEY")
    configured = bool(os.environ.get(env_name))
    return {
        "ok": configured,
        "configured": configured,
        "api_key_env": env_name,
        "base_url": config.get("base_url") or "https://api.openai.com/v1",
        "model": config.get("model") or "gpt-5.5",
        "reasoning_effort": config.get("reasoning_effort") or "low",
        "message": "OpenAI API key configured." if configured else f"OpenAI is optional. Set {env_name} to enable the openai extraction engine.",
    }


def check_sqlite_vec() -> dict[str, object]:
    try:
        import sqlite3

        conn = sqlite3.connect(":memory:")
        try:
            conn.enable_load_extension(True)
            conn.execute("select vec_version()")
            return {"available": True, "message": "sqlite-vec available"}
        finally:
            conn.close()
    except Exception:
        return {"available": False, "message": "sqlite-vec not available; BLOB embeddings will be used."}
