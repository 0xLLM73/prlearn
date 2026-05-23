from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)(token|secret|password|api[_-]?key)=\S+"),
]


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def subtract_days(value: str, days: int) -> str:
    parsed = parse_dt(value)
    if not parsed:
        return value
    return (parsed - timedelta(days=days)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_home() -> Path:
    return Path.home() / ".prlearn"


def resolve_home(path: str | None) -> Path:
    return Path(path).expanduser() if path else default_home()


def resolve_db(home: Path, db: str | None) -> Path:
    return Path(db).expanduser() if db else home / "prlearn.db"


def ensure_home(home: Path) -> None:
    for child in [home, home / "logs", home / "reports", home / "exports", home / "bin"]:
        child.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def stable_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(data: Any) -> str:
    return hashlib.sha256(stable_json(data).encode("utf-8")).hexdigest()


def normalize_text(text: str | None) -> str:
    text = (text or "").lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-z0-9#/_ .-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def slug_key(parts: list[str]) -> str:
    normalized = "|".join(normalize_text(part) for part in parts if part)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def redact(text: str | None) -> str:
    from .redaction import redact_text

    return redact_text(text).text


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run_command(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)


def python_version_ok() -> bool:
    return sys.version_info >= (3, 11)


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
