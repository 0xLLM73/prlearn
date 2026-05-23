from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .redaction import redact_text
from .util import normalize_text


GENERIC_PATTERNS = [
    re.compile(r"^(add|write|improve|update) tests?\.?$", re.I),
    re.compile(r"^(fix|run) (lint|typecheck|ci|build)\.?$", re.I),
    re.compile(r"^handle (errors?|edge cases?)\.?$", re.I),
    re.compile(r"^write better code\.?$", re.I),
]

EXTENSION_TAGS = {
    ".py": {"python", "tests", "api"},
    ".js": {"javascript", "frontend"},
    ".jsx": {"javascript", "frontend", "ui"},
    ".ts": {"typescript", "types", "frontend"},
    ".tsx": {"typescript", "types", "frontend", "ui"},
    ".go": {"go"},
    ".rs": {"rust"},
    ".rb": {"ruby"},
    ".java": {"java"},
    ".md": {"docs"},
    ".yml": {"ci", "config"},
    ".yaml": {"ci", "config"},
    ".json": {"config", "fixtures"},
}

PATH_TOPIC_TAGS = {
    "api": {"api", "server", "backend"},
    "auth": {"auth", "security"},
    "webhook": {"webhook", "security", "api"},
    "test": {"tests"},
    "tests": {"tests"},
    "fixture": {"fixtures", "tests"},
    "fixtures": {"fixtures", "tests"},
    "component": {"ui", "frontend"},
    "components": {"ui", "frontend"},
    "dashboard": {"ui", "frontend"},
    "ui": {"ui", "frontend"},
    "route": {"api", "frontend"},
    "routes": {"api", "frontend"},
    "workflow": {"ci"},
    "workflows": {"ci"},
}

CATEGORY_ORDER = ["Security", "API Behavior", "Testing", "Types and CI", "UI", "Data and Fixtures", "Process", "General"]
ACTIONABILITY_BUCKETS = ["do_before_next_pr", "recurring_risks", "needs_human_review", "low_value"]
POSITIVE_ACTIONS = {"accept", "accepted", "merged", "telegram_major", "telegram_minor", "major", "minor"}
NEGATIVE_ACTIONS = {"reject", "rejected", "telegram_not_important", "not_important"}


def tokenize(value: str | None) -> set[str]:
    return {part for part in normalize_text(value).split() if len(part) > 2}


def flatten_paths(paths: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in paths or []:
        for part in str(value).split(","):
            cleaned = part.strip()
            if cleaned:
                result.append(cleaned)
    return sorted(dict.fromkeys(result))


def path_tags(paths: list[str]) -> set[str]:
    tags: set[str] = set()
    for value in paths:
        path = Path(value)
        tags.update(EXTENSION_TAGS.get(path.suffix.lower(), set()))
        for token in re.split(r"[/_.\-\s]+", value.lower()):
            tags.update(PATH_TOPIC_TAGS.get(token, set()))
            if token:
                tags.add(token)
    return tags


def card_text(card: dict[str, Any]) -> str:
    return " ".join(
        str(value or "")
        for value in [
            card.get("title"),
            card.get("lesson"),
            card.get("prevention_rule"),
            card.get("mistake_pattern"),
            " ".join(card.get("tags") or []),
        ]
    )


def is_generic_card(card: dict[str, Any]) -> bool:
    title = str(card.get("title") or "").strip()
    prevention = str(card.get("prevention_rule") or "").strip()
    lesson = str(card.get("lesson") or "").strip()
    combined = normalize_text(" ".join([title, prevention, lesson]))
    if any(pattern.match(title) or pattern.match(prevention) or pattern.match(lesson) for pattern in GENERIC_PATTERNS):
        return True
    vague = {"add tests", "run ci", "fix lint", "handle errors", "handle edge cases", "write better code"}
    return combined in vague or len(tokenize(combined)) <= 3


def specificity_score(card: dict[str, Any]) -> float:
    text = normalize_text(card_text(card))
    score = 0.0
    if len(tokenize(text)) >= 12:
        score += 1.0
    if any(term in text for term in ["when ", "before ", "for every", "after ", "if "]):
        score += 1.0
    if any(term in text for term in ["null", "undefined", "webhook", "signature", "typecheck", "fixture", "migration", "empty", "auth"]):
        score += 1.0
    if len(card.get("tags") or []) >= 3:
        score += 0.5
    if is_generic_card(card):
        score -= 2.5
    return max(-2.5, min(score, 3.5))


def category_for(card: dict[str, Any]) -> str:
    tags = set(card.get("tags") or [])
    text = normalize_text(card_text(card))
    if tags & {"security", "auth", "webhook"} or "signature" in text:
        return "Security"
    if tags & {"api"}:
        return "API Behavior"
    if tags & {"tests", "edge-cases"}:
        return "Testing"
    if tags & {"types", "ci", "validation"}:
        return "Types and CI"
    if tags & {"ui", "frontend"}:
        return "UI"
    if tags & {"fixtures", "data"}:
        return "Data and Fixtures"
    if tags & {"process"}:
        return "Process"
    return "General"


def checklist_for(card: dict[str, Any]) -> list[str]:
    rule = str(card.get("prevention_rule") or "").strip()
    if not rule:
        return []
    parts = [rule]
    if ";" in rule:
        parts = [part.strip() for part in rule.split(";") if part.strip()]
    elif ". " in rule:
        parts = [part.strip().rstrip(".") + "." for part in rule.split(". ") if part.strip()]
    return parts[:4]


def score_card(card: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    score = 0.0
    reasons: list[str] = []
    matches: list[str] = []
    tags = set(card.get("tags") or [])
    text = normalize_text(card_text(card))
    text_tokens = tokenize(text)
    recurrence = int(card.get("recurrence_count") or 0)
    evidence = card.get("evidence") or []

    specificity = specificity_score(card)
    score += specificity * 2.0
    if specificity > 1:
        reasons.append("specific prevention rule")

    if recurrence > 1:
        score += min(4.0, math.log2(recurrence + 1) * 1.3)
        reasons.append(f"seen {recurrence} times")
    else:
        score += 0.2

    if evidence:
        score += min(2.0, len(evidence) * 0.45)
        reasons.append(f"{len(evidence)} evidence item" + ("" if len(evidence) == 1 else "s"))

    score += float(card.get("confidence") or 0.0) * 1.2
    score += min(float(card.get("severity") or 0.0), 5.0) * 0.25

    repo = context.get("repo")
    contexts = set(card.get("contexts") or [])
    evidence_repos = {item.get("repo_full_name") for item in evidence if item.get("repo_full_name")}
    if repo and (repo in contexts or repo in evidence_repos):
        score += 4.0
        matches.append(f"repo {repo}")

    all_paths = flatten_paths((context.get("paths") or []) + (context.get("changed_files") or []))
    topic_tags = set(context.get("topic_tags") or set())
    topic_tags.update(path_tags(all_paths))
    task_tokens = tokenize(context.get("task"))
    topic_tags.update(task_tokens)

    if all_paths:
        evidence_paths = [str(item.get("path") or "") for item in evidence if item.get("path")]
        for path in all_paths:
            path_norm = normalize_text(path)
            stem_tokens = tokenize(path_norm.replace("/", " "))
            if any(path and ev_path and (path == ev_path or path.endswith(ev_path) or ev_path.endswith(path)) for ev_path in evidence_paths):
                score += 4.0
                matches.append(f"path {path}")
                break
            overlap = stem_tokens & text_tokens
            if overlap:
                score += min(2.0, len(overlap) * 0.5)
                matches.append("path topic " + ", ".join(sorted(overlap)[:3]))
                break

    tag_matches = sorted(tags & topic_tags)
    if tag_matches:
        score += min(4.0, len(tag_matches) * 1.0)
        matches.append("tag " + ", ".join(tag_matches[:4]))

    task_overlap = sorted(task_tokens & text_tokens)
    if task_overlap:
        score += min(3.0, len(task_overlap) * 0.45)
        matches.append("task " + ", ".join(task_overlap[:4]))

    recency_bonus = recency_score(card.get("last_seen_at"))
    if recency_bonus:
        score += recency_bonus
        reasons.append("recently seen")

    if is_generic_card(card):
        strong_match = bool(matches) and (repo and (repo in contexts or repo in evidence_repos) or len(tag_matches) >= 2 or any(item.startswith("path ") for item in matches))
        penalty = 1.0 if strong_match else 5.0
        score -= penalty
        reasons.append("generic lesson penalty" if penalty > 1 else "generic but context matched")

    score = round(score, 3)
    applies = matches + [reason for reason in reasons if reason not in matches]
    return {
        "card": card,
        "score": score,
        "specificity": round(specificity, 3),
        "category": category_for(card),
        "is_generic": is_generic_card(card),
        "why": why_it_matters(card),
        "applies_because": applies or ["highest available local memory match"],
        "checklist": checklist_for(card),
    }


def recency_score(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        seen = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    age_days = (datetime.now(UTC) - seen).days
    if age_days <= 30:
        return 0.8
    if age_days <= 180:
        return 0.3
    return 0.0


def why_it_matters(card: dict[str, Any]) -> str:
    lesson = str(card.get("lesson") or "").strip()
    if lesson:
        return lesson
    return str(card.get("prevention_rule") or "").strip()


def rank_cards(cards: list[dict[str, Any]], context: dict[str, Any] | None = None, *, top: int = 10, min_score: float = 0.5) -> list[dict[str, Any]]:
    scored = [score_card(card, context) for card in cards]
    scored = [item for item in scored if item["score"] >= min_score]
    scored.sort(
        key=lambda item: (
            item["score"],
            int(item["card"].get("recurrence_count") or 0),
            float(item["card"].get("confidence") or 0.0),
            str(item["card"].get("title") or ""),
        ),
        reverse=True,
    )
    return scored[:top]


def summarize_evidence(evidence: list[dict[str, Any]], *, verbose: bool = False, limit: int = 2) -> list[dict[str, Any]]:
    items = []
    for item in evidence[:limit if not verbose else max(limit, 5)]:
        quote = redact_text(item.get("quote") or "").text.replace("\n", " ").strip()
        if len(quote) > 220:
            quote = quote[:217].rstrip() + "..."
        items.append(
            {
                "repo_full_name": item.get("repo_full_name"),
                "pr_number": item.get("pr_number"),
                "path": item.get("path"),
                "url": item.get("url"),
                "quote": quote if verbose else "",
            }
        )
    return items


def card_summary(card: dict[str, Any], *, verbose: bool = False) -> dict[str, Any]:
    scored = score_card(card)
    actionability = actionability_for_card(card, scored=scored)
    return {
        "id": card["id"],
        "title": card["title"],
        "status": card["status"],
        "score": scored["score"],
        "category": scored["category"],
        "is_generic": scored["is_generic"],
        "recurrence_count": card["recurrence_count"],
        "confidence": card["confidence"],
        "severity": card["severity"],
        "tags": card["tags"],
        "lesson": card["lesson"],
        "prevention_rule": card["prevention_rule"],
        "evidence_count": len(card.get("evidence") or []),
        "suggested_action": suggested_card_action(card, scored),
        "actionability": actionability,
        "routes": actionability["routes"],
        "rule": actionable_rule(card),
        "evidence": summarize_evidence(card.get("evidence") or [], verbose=verbose),
    }


def candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    evidence_count = len(candidate.get("evidence") or [])
    generic = is_generic_card(candidate)
    ready = candidate.get("status") == "pending" and evidence_count > 0 and float(candidate.get("confidence") or 0) >= 0.65 and not generic
    actionability = actionability_for_candidate(candidate)
    if candidate.get("duplicate_of_card_id"):
        action = f"merge into card {candidate['duplicate_of_card_id']}"
    elif generic:
        action = "reject or merge: too generic"
    elif ready:
        action = "accept"
    elif evidence_count == 0:
        action = "reject: weak evidence"
    else:
        action = "review"
    return {
        "id": candidate["id"],
        "title": candidate["title"],
        "status": candidate["status"],
        "confidence": candidate["confidence"],
        "severity": candidate["severity"],
        "learning_type": candidate.get("learning_type"),
        "tags": candidate.get("tags") or [],
        "lesson": candidate["lesson"],
        "prevention_rule": candidate["prevention_rule"],
        "evidence_count": evidence_count,
        "is_generic": generic,
        "ready": ready,
        "duplicate_of_card_id": candidate.get("duplicate_of_card_id"),
        "suggested_action": action,
        "actionability": actionability,
        "routes": actionability["routes"],
        "rule": actionable_rule(candidate),
        "evidence": summarize_evidence(candidate.get("evidence") or [], verbose=True, limit=2),
    }


def suggested_card_action(card: dict[str, Any], scored: dict[str, Any]) -> str:
    if card["status"] == "pending":
        if scored["is_generic"]:
            return "reject or edit before accepting"
        if len(card.get("evidence") or []) == 0:
            return "reject: weak evidence"
        return "accept if this rule still feels true"
    if card["status"] == "accepted" and scored["is_generic"]:
        return "consider merging or archiving"
    return "keep"


def readiness_score(candidate: dict[str, Any]) -> float:
    score = float(candidate.get("confidence") or 0) * 5.0
    score += min(3, int(candidate.get("evidence_count") or len(candidate.get("evidence") or [])))
    if is_generic_card(candidate):
        score -= 3.0
    if candidate.get("duplicate_of_card_id"):
        score += 1.0
    feedback = candidate.get("feedback") or {}
    score += feedback_signal_score(feedback)
    return round(score, 3)


def feedback_signal_score(feedback: dict[str, Any]) -> float:
    ratings = Counter(feedback.get("ratings") or {})
    actions = Counter(feedback.get("actions") or {})
    score = 0.0
    score += ratings.get("major", 0) * 2.5
    score += ratings.get("minor", 0) * 0.75
    score -= ratings.get("not_important", 0) * 4.0
    score += sum(actions.get(action, 0) for action in POSITIVE_ACTIONS) * 0.5
    score -= sum(actions.get(action, 0) for action in NEGATIVE_ACTIONS) * 2.0
    return score


def feedback_summary(conn: sqlite3.Connection) -> dict[str, dict[Any, dict[str, Counter[str]]]]:
    result: dict[str, dict[Any, dict[str, Counter[str]]]] = {"cards": {}, "candidates": {}}
    for row in conn.execute("select learning_id, candidate_id, action from learning_card_feedback").fetchall():
        action = str(row["action"] or "")
        if row["learning_id"] is not None:
            entry = result["cards"].setdefault(int(row["learning_id"]), {"actions": Counter(), "ratings": Counter()})
            entry["actions"][action] += 1
        if row["candidate_id"]:
            entry = result["candidates"].setdefault(str(row["candidate_id"]), {"actions": Counter(), "ratings": Counter()})
            entry["actions"][action] += 1
    for row in conn.execute("select learning_id, candidate_id, rating from learning_candidate_ratings").fetchall():
        rating = str(row["rating"] or "")
        if row["learning_id"] is not None:
            entry = result["cards"].setdefault(int(row["learning_id"]), {"actions": Counter(), "ratings": Counter()})
            entry["ratings"][rating] += 1
        if row["candidate_id"]:
            entry = result["candidates"].setdefault(str(row["candidate_id"]), {"actions": Counter(), "ratings": Counter()})
            entry["ratings"][rating] += 1
    return result


def attach_feedback(items: list[dict[str, Any]], feedback: dict[Any, dict[str, Counter[str]]], *, id_key: str = "id") -> list[dict[str, Any]]:
    enriched = []
    for item in items:
        copied = dict(item)
        item_feedback = feedback.get(copied[id_key], {"actions": Counter(), "ratings": Counter()})
        copied["feedback"] = {
            "actions": dict(item_feedback.get("actions") or {}),
            "ratings": dict(item_feedback.get("ratings") or {}),
        }
        enriched.append(copied)
    return enriched


def actionability_for_card(card: dict[str, Any], *, scored: dict[str, Any] | None = None) -> dict[str, Any]:
    scored = scored or score_card(card)
    evidence_count = len(card.get("evidence") or [])
    recurrence = int(card.get("recurrence_count") or 0)
    confidence = float(card.get("confidence") or 0)
    severity = int(card.get("severity") or 0)
    feedback_score = feedback_signal_score(card.get("feedback") or {})
    score = float(scored["score"]) + feedback_score
    routes: list[str] = []
    reasons: list[str] = []

    weak = evidence_count == 0 or confidence < 0.55
    generic = bool(scored.get("is_generic"))
    if card.get("status") in {"rejected", "archived"} or weak or generic:
        label = "noise"
        routes.append("low_value")
        if weak:
            reasons.append("weak evidence")
        if generic:
            reasons.append("generic rule")
    else:
        if recurrence >= 2:
            routes.append("recurring_risks")
            reasons.append(f"seen {recurrence} times")
        if card.get("status") == "pending":
            routes.append("needs_human_review")
            reasons.append("pending card")
        if card.get("status") == "accepted":
            routes.append("do_before_next_pr")
        if severity >= 4:
            reasons.append("high severity")
        if feedback_score > 0:
            reasons.append("positive human feedback")
        if recurrence >= 3 or severity >= 4 or score >= 8:
            label = "major"
        else:
            label = "minor"

    if not routes:
        routes.append("low_value" if label == "noise" else "needs_human_review")
    return {
        "label": label,
        "score": round(score, 3),
        "routes": sorted(dict.fromkeys(routes), key=lambda item: ACTIONABILITY_BUCKETS.index(item)),
        "reasons": reasons or ["review before relying on this insight"],
    }


def actionability_for_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    evidence_count = len(candidate.get("evidence") or [])
    confidence = float(candidate.get("confidence") or 0)
    severity = int(candidate.get("severity") or 0)
    feedback_score = feedback_signal_score(candidate.get("feedback") or {})
    score = readiness_score(candidate)
    routes: list[str] = []
    reasons: list[str] = []

    if candidate.get("status") == "rejected" or evidence_count == 0 or is_generic_card(candidate) or feedback_score <= -3:
        label = "noise"
        routes.append("low_value")
        if evidence_count == 0:
            reasons.append("weak evidence")
        if is_generic_card(candidate):
            reasons.append("generic rule")
        if feedback_score < 0:
            reasons.append("negative human feedback")
    else:
        if candidate.get("status") == "pending":
            routes.append("needs_human_review")
            reasons.append("pending candidate")
        if confidence >= 0.75 or severity >= 4 or feedback_score > 1:
            label = "major"
        else:
            label = "minor"
        if candidate.get("duplicate_of_card_id"):
            reasons.append("matches an existing card")
        if feedback_score > 0:
            reasons.append("positive human feedback")

    return {
        "label": label,
        "score": round(score, 3),
        "routes": sorted(dict.fromkeys(routes), key=lambda item: ACTIONABILITY_BUCKETS.index(item)),
        "reasons": reasons or ["review before relying on this insight"],
    }


def trigger_for(item: dict[str, Any]) -> str:
    tags = item.get("tags") or []
    contexts = item.get("contexts") or []
    pattern = str(item.get("mistake_pattern") or "").replace("-", " ").strip()
    if tags:
        return "When working on " + ", ".join(str(tag) for tag in tags[:3])
    if contexts:
        return "When working in " + ", ".join(str(context) for context in contexts[:2])
    if pattern:
        return "When this pattern appears: " + pattern
    return "When similar evidence appears again"


def actionable_rule(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "when": trigger_for(item),
        "do": str(item.get("prevention_rule") or "").strip(),
        "because": why_it_matters(item),
        "applies_to": {
            "tags": item.get("tags") or [],
            "contexts": item.get("contexts") or [],
        },
        "evidence_count": len(item.get("evidence") or []),
    }


def usefulness_report(conn: sqlite3.Connection) -> dict[str, Any]:
    from .export import learning_rows, row_to_dict
    from .learn import candidate_rows, candidate_to_dict

    feedback = feedback_summary(conn)
    cards = attach_feedback([row_to_dict(conn, row) for row in learning_rows(conn)], feedback["cards"])
    candidates = attach_feedback([candidate_to_dict(conn, row) for row in candidate_rows(conn)], feedback["candidates"])
    status_counts = Counter(card["status"] for card in cards)
    candidate_status_counts = Counter(candidate["status"] for candidate in candidates)
    accepted = [card for card in cards if card["status"] == "accepted"]
    pending_candidates = [candidate for candidate in candidates if candidate["status"] == "pending"]
    generic_cards = [card_summary(card) for card in accepted if is_generic_card(card)]
    weak_cards = [
        card_summary(card)
        for card in accepted
        if len(card.get("evidence") or []) == 0 or float(card.get("confidence") or 0) < 0.6
    ]
    top_recurring = sorted(
        [card_summary(card) for card in accepted if int(card.get("recurrence_count") or 0) > 1],
        key=lambda item: (item["recurrence_count"], item["confidence"]),
        reverse=True,
    )[:10]
    categories = Counter()
    for card in accepted:
        categories[category_for(card)] += 1
    ready_candidates = sorted(
        [candidate_summary(candidate) for candidate in pending_candidates],
        key=lambda item: (item["ready"], item["confidence"], item["evidence_count"]),
        reverse=True,
    )
    ready_candidates = [item for item in ready_candidates if item["ready"]][:10]
    return {
        "accepted_lessons": status_counts.get("accepted", 0),
        "pending_cards": status_counts.get("pending", 0),
        "archived_cards": status_counts.get("archived", 0),
        "rejected_cards": status_counts.get("rejected", 0),
        "pending_candidates": candidate_status_counts.get("pending", 0),
        "recurring_lessons": len(top_recurring),
        "likely_generic_lessons": len(generic_cards),
        "weak_evidence_lessons": len(weak_cards),
        "top_recurring_lessons": top_recurring,
        "common_feedback_categories": [{"category": key, "count": count} for key, count in categories.most_common()],
        "generic_lessons": generic_cards[:10],
        "weak_evidence": weak_cards[:10],
        "ready_candidates": ready_candidates,
        "actionable": actionable_insights_report_from_items(cards, candidates),
        "suggested_next_commands": suggested_next_commands(status_counts, candidate_status_counts),
    }


def actionable_insights_report(conn: sqlite3.Connection, *, limit: int = 10) -> dict[str, Any]:
    from .export import learning_rows, row_to_dict
    from .learn import candidate_rows, candidate_to_dict

    feedback = feedback_summary(conn)
    cards = attach_feedback([row_to_dict(conn, row) for row in learning_rows(conn)], feedback["cards"])
    candidates = attach_feedback([candidate_to_dict(conn, row) for row in candidate_rows(conn)], feedback["candidates"])
    return actionable_insights_report_from_items(cards, candidates, limit=limit)


def actionable_insights_report_from_items(cards: list[dict[str, Any]], candidates: list[dict[str, Any]], *, limit: int = 10) -> dict[str, Any]:
    accepted_cards = [card for card in cards if card["status"] == "accepted"]
    pending_cards = [card for card in cards if card["status"] == "pending"]
    pending_candidates = [candidate for candidate in candidates if candidate["status"] == "pending"]
    all_card_summaries = [card_summary(card, verbose=True) for card in accepted_cards + pending_cards]
    all_candidate_summaries = [candidate_summary(candidate) for candidate in pending_candidates]

    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in ACTIONABILITY_BUCKETS}
    for item in all_card_summaries:
        routed = actionable_item("card", item)
        for route in item["routes"]:
            buckets[route].append(routed)
    for item in all_candidate_summaries:
        routed = actionable_item("candidate", item)
        for route in item["routes"]:
            buckets[route].append(routed)
    for route, items in buckets.items():
        items.sort(key=lambda item: (item["actionability"]["score"], item["evidence_count"], item["title"]), reverse=True)
        buckets[route] = items[:limit]
    return {
        "summary": {key: len(value) for key, value in buckets.items()},
        "do_before_next_pr": buckets["do_before_next_pr"],
        "recurring_risks": buckets["recurring_risks"],
        "needs_human_review": buckets["needs_human_review"],
        "low_value": buckets["low_value"],
        "feedback_loop": feedback_loop_summary(cards, candidates),
        "suggested_next_commands": actionable_next_commands(buckets),
    }


def actionable_item(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": item["id"],
        "title": item["title"],
        "status": item["status"],
        "actionability": item["actionability"],
        "rule": item["rule"],
        "suggested_action": item["suggested_action"],
        "evidence_count": item["evidence_count"],
        "recurrence_count": item.get("recurrence_count", 0),
        "confidence": item["confidence"],
        "severity": item["severity"],
        "tags": item["tags"],
    }


def feedback_loop_summary(cards: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    rating_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    for item in cards + candidates:
        feedback = item.get("feedback") or {}
        rating_counts.update(feedback.get("ratings") or {})
        action_counts.update(feedback.get("actions") or {})
    return {
        "ratings": dict(rating_counts),
        "review_actions": dict(action_counts),
        "human_feedback_items": sum(1 for item in cards + candidates if (item.get("feedback") or {}).get("ratings") or (item.get("feedback") or {}).get("actions")),
    }


def actionable_next_commands(buckets: dict[str, list[dict[str, Any]]]) -> list[str]:
    commands = []
    if buckets["needs_human_review"]:
        if any(item["kind"] == "candidate" for item in buckets["needs_human_review"]):
            commands.append("prlearn review --candidates --ready --limit 10")
        if any(item["kind"] == "card" for item in buckets["needs_human_review"]):
            commands.append("prlearn review --limit 10")
    if buckets["do_before_next_pr"]:
        commands.append("prlearn preflight --include-pending")
    if buckets["low_value"]:
        if any(item["kind"] == "candidate" for item in buckets["low_value"]):
            commands.append("prlearn review --candidates --min-score 0 --limit 10")
        if any(item["kind"] == "card" for item in buckets["low_value"]):
            commands.append("prlearn review --limit 10")
    commands.append("prlearn export")
    return commands


def suggested_next_commands(status_counts: Counter[str], candidate_status_counts: Counter[str]) -> list[str]:
    commands = []
    if candidate_status_counts.get("pending", 0):
        commands.append("prlearn review --candidates --limit 10")
    if status_counts.get("pending", 0):
        commands.append("prlearn review --limit 10")
    commands.extend(["prlearn preflight", "prlearn export"])
    return commands


def render_usefulness_report(report: dict[str, Any]) -> str:
    lines = [
        "prlearn usefulness report",
        "",
        f"Accepted lessons: {report['accepted_lessons']}",
        f"Pending candidates: {report['pending_candidates']}",
        f"Recurring lessons: {report['recurring_lessons']}",
        f"Likely generic lessons: {report['likely_generic_lessons']}",
        f"Weak-evidence lessons: {report['weak_evidence_lessons']}",
        "",
        "Top recurring lessons:",
    ]
    if report["top_recurring_lessons"]:
        for index, card in enumerate(report["top_recurring_lessons"], 1):
            lines.append(f"{index}. {card['title']}")
            lines.append(f"   Seen {card['recurrence_count']} times; evidence items: {card['evidence_count']}")
    else:
        lines.append("_No recurring accepted lessons yet._")
    lines.extend(["", "Needs attention:"])
    if report["ready_candidates"]:
        lines.append(f"- {len(report['ready_candidates'])} candidates have reviewable evidence and are ready for review.")
    if report["generic_lessons"]:
        lines.append(f"- {len(report['generic_lessons'])} accepted cards look generic and may need merging or archiving.")
    if report["weak_evidence"]:
        lines.append(f"- {len(report['weak_evidence'])} lessons have weak evidence.")
    if not report["ready_candidates"] and not report["generic_lessons"] and not report["weak_evidence"]:
        lines.append("- No obvious cleanup issues found.")
    lines.extend(["", "Suggested next commands:"])
    for command in report["suggested_next_commands"]:
        lines.append(f"- {command}")
    return "\n".join(lines) + "\n"


def render_actionable_insights_report(report: dict[str, Any]) -> str:
    lines = [
        "prlearn actionable insights",
        "",
        f"Do before next PR: {report['summary']['do_before_next_pr']}",
        f"Recurring risks: {report['summary']['recurring_risks']}",
        f"Needs human review: {report['summary']['needs_human_review']}",
        f"Low-value or stale: {report['summary']['low_value']}",
        "",
        "Do before next PR:",
    ]
    lines.extend(render_actionable_items(report["do_before_next_pr"]))
    lines.extend(["", "Recurring risks:"])
    lines.extend(render_actionable_items(report["recurring_risks"]))
    lines.extend(["", "Needs human review:"])
    lines.extend(render_actionable_items(report["needs_human_review"]))
    lines.extend(["", "Low-value or stale:"])
    lines.extend(render_actionable_items(report["low_value"], empty="_No obvious low-value items._"))
    lines.extend(["", "Suggested next commands:"])
    for command in report["suggested_next_commands"]:
        lines.append(f"- {command}")
    return "\n".join(lines) + "\n"


def render_actionable_items(items: list[dict[str, Any]], *, empty: str = "_None yet._") -> list[str]:
    if not items:
        return [empty]
    lines = []
    for index, item in enumerate(items, 1):
        actionability = item["actionability"]
        rule = item["rule"]
        lines.append(f"{index}. [{actionability['label']}] {item['title']}")
        lines.append(f"   When: {rule['when']}")
        lines.append(f"   Do: {rule['do']}")
        lines.append(f"   Why: {rule['because']}")
        lines.append(f"   Action: {item['suggested_action']}")
    return lines
