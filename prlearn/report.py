from __future__ import annotations

import sqlite3
from pathlib import Path

from .export import learning_rows, row_to_dict
from .usefulness import actionable_insights_report
from .util import utcnow


def write_daily_report(conn: sqlite3.Connection, reports_dir: Path, stats: dict[str, object]) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    date = utcnow()[:10]
    path = reports_dir / f"{date}.md"
    rows = [row_to_dict(conn, row) for row in learning_rows(conn, limit=20)]
    lines = [
        f"# prlearn daily report: {date}",
        "",
        "## Pipeline",
        "",
    ]
    for key, value in stats.items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Current learnings", ""])
    if rows:
        for card in rows:
            lines.append(f"- {card['title']} ({card['status']}, seen {card['recurrence_count']}x)")
    else:
        lines.append("_No learnings yet._")
    path.write_text("\n".join(lines) + "\n")
    return path


def daily_summary(conn: sqlite3.Connection, stats: dict[str, object], report_path: Path | None = None) -> dict[str, object]:
    accepted = conn.execute("select count(*) as count from learning_cards where status='accepted'").fetchone()["count"]
    pending_cards = conn.execute("select count(*) as count from learning_cards where status='pending'").fetchone()["count"]
    pending_candidates = conn.execute("select count(*) as count from learning_candidates where status='pending'").fetchone()["count"]
    created = int(stats.get("created") or 0)
    updated = int(stats.get("updated") or 0)
    candidates_created = int(stats.get("candidates_created") or stats.get("candidates") or 0)
    actionable = actionable_insights_report(conn, limit=3)
    suggested = actionable["suggested_next_commands"][0] if actionable["suggested_next_commands"] else "prlearn preflight"
    if pending_candidates and not actionable["summary"]["needs_human_review"]:
        suggested = "prlearn review --candidates --limit 10"
    elif pending_cards and not pending_candidates:
        suggested = "prlearn review --limit 10"
    return {
        "synced_prs": int(stats.get("prs_seen") or 0),
        "changed_prs": int(stats.get("prs_changed") or 0),
        "events_upserted": int(stats.get("events_upserted") or 0),
        "new_learning_cards": created,
        "updated_recurring_lessons": updated,
        "new_candidates": candidates_created,
        "accepted_cards": int(accepted),
        "pending_cards": int(pending_cards),
        "pending_candidates": int(pending_candidates),
        "actionable": actionable["summary"],
        "report": str(report_path) if report_path else None,
        "suggested_next_command": suggested,
    }


def render_daily_summary(summary: dict[str, object]) -> str:
    lines = [
        "Daily complete.",
        f"Synced PRs: {summary['synced_prs']} ({summary['changed_prs']} changed)",
        f"Events upserted: {summary['events_upserted']}",
        f"New learning cards: {summary['new_learning_cards']}",
        f"Updated recurring lessons: {summary['updated_recurring_lessons']}",
        f"New candidates: {summary['new_candidates']}",
        f"Accepted cards: {summary['accepted_cards']}",
        f"Needs review: {summary['pending_candidates']} candidates, {summary['pending_cards']} cards",
        f"Actionable now: {summary['actionable']['do_before_next_pr']} do-before-next-PR, {summary['actionable']['recurring_risks']} recurring risks",
    ]
    if summary.get("report"):
        lines.append(f"Report: {summary['report']}")
    lines.append(f"Next: {summary['suggested_next_command']}")
    return "\n".join(lines)
