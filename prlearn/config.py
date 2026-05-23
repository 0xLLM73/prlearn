from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .util import read_json, utcnow, write_json


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 4,
    "privacy": {
        "encrypt_raw_json": False,
        "passphrase_env": "PRLEARN_PASSPHRASE",
    },
    "github": {
        "auth_mode": "auto",
        "api_url": "https://api.github.com",
        "app_id": "",
        "app_id_env": "PRLEARN_GITHUB_APP_ID",
        "installation_id": "",
        "installation_id_env": "PRLEARN_GITHUB_INSTALLATION_ID",
        "private_key_file": "",
        "private_key_file_env": "PRLEARN_GITHUB_APP_PRIVATE_KEY_FILE",
        "private_key_env": "PRLEARN_GITHUB_APP_PRIVATE_KEY",
        "default_author": "",
        "default_author_env": "PRLEARN_GITHUB_AUTHOR",
    },
    "ollama": {
        "base_url": "http://127.0.0.1:11434",
        "require_localhost": True,
        "strict_local": True,
        "request_timeout_seconds": 180,
        "models": {
            "embedder": "qwen3-embedding:0.6b",
            "classifier": "qwen3.5:2b",
            "extractor": "qwen3.5:9b",
            "judge": "qwen3.5:9b",
        },
        "options": {
            "temperature": 0,
            "seed": 42,
            "top_p": 0.1,
            "num_ctx": 8192,
            "num_predict": 2048,
            "stream": False,
            "think": False,
        },
    },
    "codex": {
        "command": "codex",
        "request_timeout_seconds": 600,
        "model": "gpt-5.5",
        "reasoning_effort": "low",
        "profile": "",
        "extra_args": [],
        "include_raw_metadata": True,
    },
    "openai": {
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "request_timeout_seconds": 180,
        "model": "gpt-5.5",
        "reasoning_effort": "low",
        "max_output_tokens": 2048,
        "include_raw_metadata": True,
    },
}


def default_config(db_path: Path) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["db"] = str(db_path)
    config["created_at"] = utcnow()
    return config


def merge_defaults(config: dict[str, Any], defaults: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
    merged = copy.deepcopy(config)
    changed = False
    source = DEFAULT_CONFIG if defaults is None else defaults
    for key, value in source.items():
        if key not in merged:
            merged[key] = copy.deepcopy(value)
            changed = True
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            child, child_changed = merge_defaults(merged[key], value)
            merged[key] = child
            changed = changed or child_changed
    return merged, changed


def load_config(home: Path, db_path: Path, *, write_back: bool = True) -> dict[str, Any]:
    path = home / "config.json"
    if path.exists():
        config = read_json(path)
        if not isinstance(config, dict):
            config = {}
    else:
        config = default_config(db_path)
        write_json(path, config)
        return config
    if "db" not in config:
        config["db"] = str(db_path)
    merged, changed = merge_defaults(config)
    if changed and write_back:
        write_json(path, merged)
    return merged


def save_config(home: Path, config: dict[str, Any]) -> None:
    write_json(home / "config.json", config)


def ollama_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("ollama") or {}
    if not isinstance(value, dict):
        value = {}
    merged, _ = merge_defaults({"ollama": value})
    return merged["ollama"]


def github_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("github") or {}
    if not isinstance(value, dict):
        value = {}
    merged, _ = merge_defaults({"github": value})
    return merged["github"]


def codex_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("codex") or {}
    if not isinstance(value, dict):
        value = {}
    merged, _ = merge_defaults({"codex": value})
    return merged["codex"]


def openai_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("openai") or {}
    if not isinstance(value, dict):
        value = {}
    merged, _ = merge_defaults({"openai": value})
    return merged["openai"]
