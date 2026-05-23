#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_PEM_BEGIN = "BEGIN "
_PEM_KIND = "PRIVATE KEY"
_PEM_END = "END "
_MACOS_USERS_ROOT = "/" + "Users" + "/"


@dataclass(frozen=True)
class Finding:
    severity: str
    check: str
    location: str
    message: str


def run_git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git command failed")
    return result.stdout


def tracked_files() -> list[Path]:
    raw = run_git(["ls-files", "-z"])
    return [ROOT / value for value in raw.split("\0") if value]


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def secret_patterns() -> list[tuple[str, re.Pattern[str]]]:
    pem_pattern = r"-----" + _PEM_BEGIN + r"[A-Z0-9 ]*" + _PEM_KIND + r"-----"
    pem_pattern += r"|-----" + _PEM_END + r"[A-Z0-9 ]*" + _PEM_KIND + r"-----"
    return [
        ("GitHub classic token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
        ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
        ("OpenAI style API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
        ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}\b")),
        ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
        ("Telegram bot token", re.compile(r"\b\d{8,12}:AA[A-Za-z0-9_-]{30,}\b")),
        ("Private key block", re.compile(pem_pattern)),
        ("credential URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")),
        ("local macOS user path", re.compile(re.escape(_MACOS_USERS_ROOT) + r"[A-Za-z0-9._-]+/")),
    ]


def check_tracked_content() -> list[Finding]:
    findings: list[Finding] = []
    patterns = secret_patterns()
    for path in tracked_files():
        text = read_text(path)
        if text is None:
            continue
        rel = path.relative_to(ROOT)
        for line_no, line in enumerate(text.splitlines(), start=1):
            for name, pattern in patterns:
                if pattern.search(line):
                    findings.append(
                        Finding(
                            "fail",
                            "tracked-content",
                            f"{rel}:{line_no}",
                            f"Potential {name} pattern in tracked file.",
                        )
                    )
    return findings


def check_ignored_state() -> list[Finding]:
    ignored = run_git(["status", "--short", "--ignored"])
    findings: list[Finding] = []
    ignored_cache_markers = (
        ".venv/",
        ".pytest_cache/",
        "prlearn.egg-info/",
        "__pycache__/",
    )
    for line in ignored.splitlines():
        if line.startswith("?? "):
            findings.append(
                Finding(
                    "warn",
                    "working-tree",
                    line[3:],
                    "Untracked file is present; confirm it is intentional before publishing.",
                )
            )
        if line.startswith("!! "):
            path = line[3:]
            if path.startswith(ignored_cache_markers) or any(marker in path for marker in ("/__pycache__/",)):
                continue
            findings.append(
                Finding(
                    "warn",
                    "ignored-state",
                    path,
                    "Ignored local file exists; confirm it is not copied into a public mirror.",
                )
            )
    return findings


def allowed_email(email: str) -> bool:
    email = email.lower()
    return email.endswith("@users.noreply.github.com") or email in {
        "noreply@github.com",
        "actions@github.com",
    }


def check_history(strict: bool) -> list[Finding]:
    rows = run_git(["log", "--all", "--format=%H%x09%an%x09%ae%x09%cn%x09%ce"]).splitlines()
    findings: list[Finding] = []
    seen: set[str] = set()
    for row in rows:
        commit, author, author_email, committer, committer_email = row.split("\t", 4)
        identities = [
            ("author", author, author_email),
            ("committer", committer, committer_email),
        ]
        for role, name, email in identities:
            identity = f"{role}:{name} <{email}>"
            if identity in seen or allowed_email(email):
                continue
            seen.add(identity)
            findings.append(
                Finding(
                    "fail" if strict else "warn",
                    "git-history",
                    commit[:12],
                    f"Non-noreply {role} metadata is present in git history.",
                )
            )
    return findings


def check_remote_branches() -> list[Finding]:
    try:
        branches = run_git(["branch", "-r", "--format=%(refname:short)"]).splitlines()
    except RuntimeError:
        return []
    findings: list[Finding] = []
    for branch in branches:
        if branch in {"origin", "origin/main", "origin/HEAD"}:
            continue
        findings.append(
            Finding(
                "warn",
                "remote-branch",
                branch,
                "Remote branch exists; delete stale private branches before direct publication.",
            )
        )
    return findings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check whether prlearn is ready for public release.")
    parser.add_argument(
        "--strict-history",
        action="store_true",
        help="Fail on non-noreply git author metadata instead of reporting it as a warning.",
    )
    parser.add_argument("--json", action="store_true", help="Print findings as JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    findings = [
        *check_tracked_content(),
        *check_ignored_state(),
        *check_history(strict=args.strict_history),
        *check_remote_branches(),
    ]
    if args.json:
        print(json.dumps([finding.__dict__ for finding in findings], indent=2, sort_keys=True))
    else:
        if not findings:
            print("Public release check passed.")
        for finding in findings:
            print(f"[{finding.severity.upper()}] {finding.check} {finding.location}: {finding.message}")
    return 1 if any(finding.severity == "fail" for finding in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
