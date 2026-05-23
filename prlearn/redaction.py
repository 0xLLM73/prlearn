from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RedactionResult:
    text: str
    hits: dict[str, int] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.hits)


def _placeholder(kind: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"[REDACTED_{kind.upper()}_{digest}]"


def redact_identifier(kind: str, value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return text
    if text.startswith("[REDACTED_"):
        return text
    return _placeholder(kind, text)


def _replace(pattern: re.Pattern[str], kind: str, text: str, hits: dict[str, int], group: int | None = None) -> str:
    def repl(match: re.Match[str]) -> str:
        if group is None:
            value = match.group(0)
            if "[REDACTED_" in value:
                return value
            hits[kind] = hits.get(kind, 0) + 1
            return _placeholder(kind, value)
        prefix = match.group(1)
        secret = match.group(group)
        if secret.startswith("[REDACTED_"):
            return match.group(0)
        hits[kind] = hits.get(kind, 0) + 1
        return f"{prefix}{_placeholder(kind, secret)}"

    return pattern.sub(repl, text)


_PEM_BEGIN = "BEGIN "
_PEM_KIND = "PRIVATE" + " KEY"
_PEM_END = "END "
_PEM_PATTERN = r"-----" + _PEM_BEGIN + r"[A-Z0-9 ]*" + _PEM_KIND + r"-----"
_PEM_PATTERN += r".*?-----" + _PEM_END + r"[A-Z0-9 ]*" + _PEM_KIND + r"-----"


PATTERNS: list[tuple[str, re.Pattern[str], int | None]] = [
    (
        "private_key",
        re.compile(_PEM_PATTERN, re.S),
        None,
    ),
    ("github_token", re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}\b"), None),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), None),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), None),
    (
        "auth_header",
        re.compile(r"(?i)(\bauthorization\s*:\s*(?:bearer|basic)\s+)([A-Za-z0-9._~+/=-]{8,})"),
        2,
    ),
    (
        "api_key_header",
        re.compile(r"(?i)(\b(?:x-api-key|api-key)\s*:\s*)([A-Za-z0-9._~+/=-]{8,})"),
        2,
    ),
    (
        "cookie",
        re.compile(r"(?i)(\bcookie\s*:\s*)([^\n]*(?:session|sid|token|auth)[^\n]*)"),
        2,
    ),
    (
        "session_cookie",
        re.compile(r"(?i)\b((?:session|sid|connect\.sid|auth_token)\s*=\s*)([^;\s]{8,})"),
        2,
    ),
    (
        "db_url",
        re.compile(r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb|redis)://[^:\s/@]+:[^@\s]+@[^\s'\"`]+", re.I),
        None,
    ),
    (
        "env_secret",
        re.compile(
            r"(?im)^(\s*(?:[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASS|API_KEY|AUTH|WEBHOOK)[A-Z0-9_]*|DATABASE_URL)\s*=\s*)([^\s#]+)"
        ),
        2,
    ),
    (
        "inline_secret",
        re.compile(r"(?i)\b((?:token|secret|password|api[_-]?key|webhook[_-]?secret)\s*[:=]\s*)([^\s,'\"`]+)"),
        2,
    ),
]


def redact_text(text: str | None) -> RedactionResult:
    value = text or ""
    hits: dict[str, int] = {}
    redacted = value
    for kind, pattern, group in PATTERNS:
        redacted = _replace(pattern, kind, redacted, hits, group)
    return RedactionResult(redacted, hits)


def contains_secret(text: str | None) -> bool:
    return redact_text(text).changed
