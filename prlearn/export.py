from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .redaction import redact_identifier, redact_text
from .usefulness import CATEGORY_ORDER, category_for, checklist_for
from .util import utcnow


def learning_rows(conn: sqlite3.Connection, status: str | None = None, tag: str | None = None, repo: str | None = None, limit: int | None = None) -> list[sqlite3.Row]:
    rows = conn.execute(
        "select * from learning_cards order by status, recurrence_count desc, severity desc, updated_at desc"
    ).fetchall()
    filtered = []
    for row in rows:
        tags = json.loads(row["tags_json"])
        contexts = json.loads(row["contexts_json"])
        if status and row["status"] != status:
            continue
        if tag and tag not in tags:
            continue
        if repo and repo not in contexts:
            continue
        filtered.append(row)
    return filtered[:limit] if limit else filtered


def evidence_for(conn: sqlite3.Connection, learning_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        select le.*, e.path, e.kind as event_kind, e.check_name, e.review_state
        from learning_evidence le
        left join events e on e.id = le.event_id
        where le.learning_id=?
        order by le.created_at, le.id
        """,
        (learning_id,),
    ).fetchall()


def row_to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    evidence = evidence_for(conn, int(row["id"]))
    return {
        "id": row["id"],
        "canonical_key": row["canonical_key"],
        "title": row["title"],
        "lesson": row["lesson"],
        "mistake_pattern": row["mistake_pattern"],
        "prevention_rule": row["prevention_rule"],
        "tags": json.loads(row["tags_json"]),
        "contexts": json.loads(row["contexts_json"]),
        "severity": row["severity"],
        "confidence": row["confidence"],
        "status": row["status"],
        "recurrence_count": row["recurrence_count"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "evidence": [
            {
                "repo_full_name": item["repo_full_name"],
                "pr_number": item["pr_number"],
                "quote": redact_text(item["quote"]).text,
                "url": item["url"],
                "path": item["path"],
                "event_kind": item["event_kind"],
                "check_name": item["check_name"],
                "review_state": item["review_state"],
            }
            for item in evidence
        ],
    }


def public_card_dict(card: dict[str, Any], *, include_evidence: bool = True) -> dict[str, Any]:
    public = {
        key: value
        for key, value in card.items()
        if key not in {"contexts", "evidence"}
    }
    public["contexts"] = [redact_identifier("repo", context) for context in card.get("contexts") or []]
    if include_evidence:
        public["evidence"] = [
            {
                "source_ref": redact_identifier(
                    "evidence",
                    "|".join(
                        str(part or "")
                        for part in [
                            item.get("repo_full_name"),
                            item.get("pr_number"),
                            item.get("url"),
                            item.get("path"),
                        ]
                    ),
                ),
                "quote": redact_text(item.get("quote")).text,
                "event_kind": item.get("event_kind"),
                "check_name": redact_identifier("check", item.get("check_name")) if item.get("check_name") else None,
                "review_state": item.get("review_state"),
            }
            for item in card.get("evidence") or []
        ]
    return public


def export_all(conn: sqlite3.Connection, out_dir: Path, fmt: str = "all", *, include_pending: bool = False) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = learning_rows(conn, status=None if include_pending else "accepted")
    if include_pending:
        rows = [row for row in rows if row["status"] in {"accepted", "pending"}]
    data = [public_card_dict(row_to_dict(conn, row)) for row in rows]
    written: dict[str, str] = {}
    if fmt in {"all", "markdown"}:
        learnings = out_dir / "LEARNINGS.md"
        rules = out_dir / "rules.md"
        learnings.write_text(render_learnings(data))
        rules.write_text(render_rules(data))
        written["LEARNINGS.md"] = str(learnings)
        written["rules.md"] = str(rules)
    if fmt in {"all", "json"}:
        context = out_dir / "context.json"
        context.write_text(json.dumps({"learnings": data}, indent=2, sort_keys=True) + "\n")
        written["context.json"] = str(context)
    return written


def render_learnings(cards: list[dict[str, Any]], *, generated_at: str | None = None) -> str:
    generated_at = generated_at or utcnow()
    lines = [
        "# prlearn memory",
        "",
        f"Generated: {generated_at}",
        "",
        "Local project memory. Use these lessons before coding or reviewing; do not treat them as external policy.",
        "",
        "Use these lessons before coding or reviewing.",
        "",
    ]
    if not cards:
        lines.append("_No learnings yet._")
        return "\n".join(lines) + "\n"
    cards = [card for card in cards if card["status"] == "accepted"]
    if not cards:
        lines.append("_No accepted learnings yet._")
        return "\n".join(lines) + "\n"
    groups: dict[str, list[dict[str, Any]]] = {}
    for card in sorted(cards, key=lambda item: (category_for(item), -int(item["recurrence_count"]), item["title"])):
        groups.setdefault(category_for(card), []).append(card)
    ordered_categories = [category for category in CATEGORY_ORDER if category in groups]
    ordered_categories.extend(sorted(set(groups) - set(ordered_categories)))
    for category in ordered_categories:
        lines.extend([f"## {category}", ""])
        for card in groups[category]:
            contexts = sorted(set(card.get("contexts") or []))
            applies = ", ".join(contexts[:4]) if contexts else ", ".join(card.get("tags") or [])
            if len(contexts) > 4:
                applies += f", +{len(contexts) - 4} more"
            lines.extend(
                [
                    f"### {card['title']}",
                    f"Seen: {card['recurrence_count']} time" + ("" if card["recurrence_count"] == 1 else "s"),
                    f"Applies when: {applies}",
                    "",
                    "Checklist:",
                ]
            )
            for action in checklist_for(card) or [card["prevention_rule"]]:
                lines.append(f"- {action}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_rules(cards: list[dict[str, Any]]) -> str:
    accepted = [card for card in cards if card["status"] == "accepted"]
    lines = ["# Personal Coding Rules", ""]
    for card in accepted:
        lines.append(f"- {card['prevention_rule']}")
    if len(lines) == 2:
        lines.append("_No accepted rules yet._")
    return "\n".join(lines) + "\n"
