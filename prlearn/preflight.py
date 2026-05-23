from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from .export import learning_rows, public_card_dict, row_to_dict
from .redaction import redact_identifier
from .usefulness import flatten_paths, path_tags, rank_cards, summarize_evidence


def run_git(path: Path, args: list[str], *, timeout: int = 10) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["git", "-C", str(path), *args], text=True, capture_output=True, timeout=timeout, check=False)
    except Exception:
        return None


def detect_git_root(path: Path) -> Path | None:
    result = run_git(path, ["rev-parse", "--show-toplevel"])
    if not result or result.returncode != 0:
        return None
    value = result.stdout.strip()
    return Path(value) if value else None


def detect_branch(path: Path) -> str | None:
    result = run_git(path, ["branch", "--show-current"])
    if result and result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    result = run_git(path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if result and result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def detect_repo(path: Path) -> str | None:
    result = run_git(path, ["remote", "get-url", "origin"])
    if not result or result.returncode != 0:
        return None
    remote = result.stdout.strip()
    if remote.endswith(".git"):
        remote = remote[:-4]
    if remote.startswith("git@github.com:"):
        return remote.split(":", 1)[1]
    if "github.com/" in remote:
        return remote.rsplit("github.com/", 1)[1]
    return None


def changed_files(path: Path) -> list[str]:
    files: list[str] = []
    for args in [
        ["diff", "--name-only", "--cached"],
        ["diff", "--name-only"],
        ["ls-files", "--others", "--exclude-standard"],
    ]:
        result = run_git(path, args)
        if result and result.returncode == 0:
            files.extend(line.strip() for line in result.stdout.splitlines() if line.strip())
    return sorted(dict.fromkeys(files))


def preflight_context(repo_path: Path, task: str = "", paths: list[str] | None = None) -> dict[str, Any]:
    base = repo_path.expanduser()
    explicit_paths = flatten_paths(paths)
    root = detect_git_root(base) if base.exists() else None
    if root:
        changed = changed_files(root)
        repo = detect_repo(root)
        branch = detect_branch(root)
        git_detected = True
    else:
        changed = []
        repo = None
        branch = None
        git_detected = False
    effective_paths = explicit_paths or changed
    return {
        "repo_path": str(base),
        "git_detected": git_detected,
        "git_root": str(root) if root else None,
        "repo": repo,
        "branch": branch,
        "changed_files": changed,
        "paths": effective_paths,
        "explicit_paths": explicit_paths,
        "task": task,
        "topic_tags": sorted(path_tags(effective_paths) | set(task.split())),
    }


def preflight_report(
    conn: sqlite3.Connection,
    repo_path: Path,
    *,
    task: str = "",
    paths: list[str] | None = None,
    top: int = 10,
    include_pending: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    context = preflight_context(repo_path, task=task, paths=paths)
    rows = learning_rows(conn)
    cards = []
    for row in rows:
        if row["status"] in {"rejected", "archived"}:
            continue
        if row["status"] == "pending" and not include_pending:
            continue
        cards.append(row_to_dict(conn, row))
    ranked = rank_cards(cards, context, top=top)
    lessons = []
    for item in ranked:
        public_item = {key: value for key, value in item.items() if key != "card"}
        public_item["applies_because"] = public_reasons(item.get("applies_because") or [], context)
        card = dict(item["card"])
        evidence = card.pop("evidence", [])
        lessons.append(
            {
                **public_item,
                "card": public_card_dict(card, include_evidence=False),
                "evidence": public_evidence_summary(summarize_evidence(evidence, verbose=verbose)),
            }
        )
    return {
        "context": public_context(context),
        "lessons": lessons,
        "message": "No highly relevant lessons found." if not lessons else None,
    }


def public_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "git_detected": context.get("git_detected"),
        "repo": redact_identifier("repo", context.get("repo")) if context.get("repo") else None,
        "branch": redact_identifier("branch", context.get("branch")) if context.get("branch") else None,
        "repo_path": redact_identifier("path", context.get("repo_path")) if context.get("repo_path") else None,
        "git_root": redact_identifier("path", context.get("git_root")) if context.get("git_root") else None,
        "changed_files": [redact_identifier("path", path) for path in context.get("changed_files") or []],
        "paths": [redact_identifier("path", path) for path in context.get("paths") or []],
        "explicit_paths": [redact_identifier("path", path) for path in context.get("explicit_paths") or []],
        "task": context.get("task"),
        "topic_tags": sorted(tag for tag in context.get("topic_tags") or [] if "/" not in str(tag)),
    }


def public_evidence_summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source_ref": redact_identifier(
                "evidence",
                "|".join(str(part or "") for part in [item.get("repo_full_name"), item.get("pr_number"), item.get("path")]),
            ),
            "quote": item.get("quote"),
        }
        for item in items
    ]


def public_reasons(reasons: list[str], context: dict[str, Any]) -> list[str]:
    replacements: dict[str, str] = {}
    if context.get("repo"):
        replacements[str(context["repo"])] = str(redact_identifier("repo", context["repo"]))
    if context.get("branch"):
        replacements[str(context["branch"])] = str(redact_identifier("branch", context["branch"]))
    for path in (context.get("changed_files") or []) + (context.get("paths") or []) + (context.get("explicit_paths") or []):
        replacements[str(path)] = str(redact_identifier("path", path))
    public = []
    for reason in reasons:
        text = str(reason)
        for raw, redacted in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
            text = text.replace(raw, redacted)
        public.append(text)
    return public


def relevant(conn: sqlite3.Connection, repo_path: Path, task: str, top: int, include_pending: bool) -> list[dict[str, object]]:
    report = preflight_report(conn, repo_path, task=task, top=top, include_pending=include_pending)
    return [item["card"] for item in report["lessons"]]


def render_preflight(report_or_cards: dict[str, Any] | list[dict[str, object]], *, verbose: bool = False) -> str:
    if isinstance(report_or_cards, list):
        report = {"context": {}, "lessons": [{"card": card, "why": card.get("lesson"), "applies_because": [], "checklist": [card.get("prevention_rule")]} for card in report_or_cards]}
    else:
        report = report_or_cards
    lines = ["Relevant prior lessons for this work", ""]
    context = report.get("context") or {}
    if context.get("git_detected"):
        repo = context.get("repo") or "unknown remote"
        branch = context.get("branch") or "unknown branch"
        lines.append(f"Repo: {repo} ({branch})")
        paths = context.get("paths") or context.get("changed_files") or []
        if paths:
            preview = ", ".join(paths[:5])
            if len(paths) > 5:
                preview += f", +{len(paths) - 5} more"
            lines.append(f"Work context: {preview}")
        lines.append("")
    elif context:
        lines.append("No git repo detected; ranking from task and provided paths only.")
        lines.append("")

    lessons = report.get("lessons") or []
    if not lessons:
        lines.extend(
            [
                "No highly relevant lessons found.",
                "Try `prlearn list --accepted` or run `prlearn daily` to extract more lessons.",
            ]
        )
        return "\n".join(lines) + "\n"

    for index, item in enumerate(lessons, 1):
        card = item["card"]
        applies = item.get("applies_because") or []
        lines.extend(
            [
                f"{index}. {card['title']}",
                f"   Why it matters: {item.get('why') or card.get('lesson')}",
                f"   Applies because: {'; '.join(applies[:4])}",
                "   Checklist:",
            ]
        )
        checklist = item.get("checklist") or [card.get("prevention_rule")]
        for action in checklist:
            if action:
                lines.append(f"   - {action}")
        if verbose and item.get("evidence"):
            lines.append("   Evidence:")
            for evidence in item["evidence"]:
                ref = evidence.get("source_ref")
                quote = f": {evidence.get('quote')}" if evidence.get("quote") else ""
                lines.append(f"   - {ref}{quote}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
