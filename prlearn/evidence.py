from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from .chunking import chunks_for_text, stable_chunk_id
from .crypto import reveal_text
from .redaction import redact_text
from .util import content_hash, normalize_text, slug_key, stable_json, utcnow


NOISE_ONLY = re.compile(r"^(lgtm|nice work|looks good|great job|thanks|thank you|deployed preview|ci passed)$", re.I)
BOT_NOISE = re.compile(r"\b(deployed preview|ci passed)\b", re.I)
SELF_STATUS = re.compile(r"^((fixed|done|addressed|updated|resolved|pushed)( (it|this|that|thanks|thank you))?|thanks|thank you)$", re.I)
REVIEW_SIGNAL = re.compile(
    r"\b(please|missing|should|needs?|must|bug|break|throw|fail(?:ed|ing|ure)?|security|test|typecheck|validation|edge case|null|undefined|empty state|regression|guard|risk|production|staging|migration|fixture|runtime|isolation)\b",
    re.I,
)
EXPLICIT_SAFETY_CONTEXT = re.compile(
    r"\b(safety boundary|data loss|write gate|live execution|venue order|production-looking|staging-only|no live)\b",
    re.I,
)
SENSITIVE_CONTEXT_SIGNAL = re.compile(
    r"\b(security|privacy|leak|auth(?:entication|orization)?|credential|secret|token|webhook|idempotenc\w*|production|staging|fixture|isolation|read-only)\b",
    re.I,
)
HIGH_SIGNAL_DIRECTIVE = re.compile(r"\b(must|required?|refuse|prevent|avoid|guard|gate|do not|never|no live|read-only|staging-only)\b", re.I)
TEST_RISK_SIGNAL = re.compile(r"\b(regression|edge case|null|undefined|empty state|failure|failed|typecheck|lint|build|ci)\b", re.I)
FAILED_CHECK_CONCLUSIONS = {"failure", "failed", "timed_out", "action_required"}


@dataclass(frozen=True)
class EvidenceStats:
    items_created: int = 0
    chunks_created: int = 0
    redaction_hits: int = 0


def ensure_evidence_for_dirty_prs(conn: sqlite3.Connection, *, all_prs: bool = False, pr_ref: str | None = None, config: dict[str, Any] | None = None) -> dict[str, int]:
    prs = select_prs(conn, all_prs=all_prs, pr_ref=pr_ref)
    totals = {"evidence_items_created": 0, "evidence_chunks_created": 0, "redaction_hits": 0}
    for pr in prs:
        stats = ensure_evidence_for_pr(conn, pr, config=config)
        totals["evidence_items_created"] += stats.items_created
        totals["evidence_chunks_created"] += stats.chunks_created
        totals["redaction_hits"] += stats.redaction_hits
    conn.commit()
    return totals


def select_prs(conn: sqlite3.Connection, *, all_prs: bool = False, pr_ref: str | None = None) -> list[sqlite3.Row]:
    where = []
    params: list[object] = []
    if not all_prs:
        where.append("dirty = 1")
    if pr_ref:
        repo, number = parse_pr_ref(pr_ref)
        where.append("repo_full_name = ? and number = ?")
        params.extend([repo, number])
    clause = " where " + " and ".join(where) if where else ""
    return conn.execute(f"select * from prs{clause}", params).fetchall()


def parse_pr_ref(value: str) -> tuple[str, int]:
    if "#" not in value:
        raise ValueError("--pr must look like owner/repo#123")
    repo, number = value.rsplit("#", 1)
    return repo, int(number)


def ensure_evidence_for_pr(conn: sqlite3.Connection, pr: sqlite3.Row, *, config: dict[str, Any] | None = None) -> EvidenceStats:
    created_items = 0
    created_chunks = 0
    redaction_hits = 0
    for item in evidence_sources(conn, pr, config=config):
        redacted = redact_text(item["text"])
        metadata = dict(item["metadata"])
        metadata["redaction_hits"] = redacted.hits
        evidence_id = stable_evidence_id(pr, item)
        row = conn.execute("select id from evidence_items where id=?", (evidence_id,)).fetchone()
        if row is None:
            created_items += 1
        conn.execute(
            """
            insert into evidence_items(id, repo_full_name, pr_number, source_table, source_id, source_kind,
              actor_login, actor_role, is_primary_signal, is_self_authored, is_bot, is_noise, signal_score,
              title, url, path, line, created_at, updated_at, redacted_text, redaction_hits_json, metadata_json, content_hash)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            on conflict(id) do update set
              actor_login=excluded.actor_login, actor_role=excluded.actor_role,
              is_primary_signal=excluded.is_primary_signal, is_self_authored=excluded.is_self_authored,
              is_bot=excluded.is_bot, is_noise=excluded.is_noise, signal_score=excluded.signal_score,
              title=excluded.title, url=excluded.url, path=excluded.path, line=excluded.line,
              updated_at=excluded.updated_at, redacted_text=excluded.redacted_text,
              redaction_hits_json=excluded.redaction_hits_json, metadata_json=excluded.metadata_json,
              content_hash=excluded.content_hash
            """,
            (
                evidence_id,
                pr["repo_full_name"],
                pr["number"],
                item["source_table"],
                str(item["source_id"]),
                item["source_kind"],
                item.get("actor_login"),
                item["actor_role"],
                1 if item["is_primary_signal"] else 0,
                1 if item["is_self_authored"] else 0,
                1 if item["is_bot"] else 0,
                1 if item["is_noise"] else 0,
                item["signal_score"],
                item.get("title"),
                item.get("url"),
                item.get("path"),
                item.get("line"),
                item.get("created_at") or utcnow(),
                item.get("updated_at") or item.get("created_at") or utcnow(),
                redacted.text,
                json.dumps(redacted.hits, sort_keys=True),
                stable_json(metadata),
                content_hash({"text": redacted.text, "metadata": metadata}),
            ),
        )
        redaction_hits += sum(redacted.hits.values())
        for index, text in enumerate(chunks_for_text(redacted.text, kind=item["source_kind"])):
            chunk_id = stable_chunk_id(evidence_id, index, text)
            existed = conn.execute("select id from evidence_chunks where id=?", (chunk_id,)).fetchone()
            if existed is None:
                created_chunks += 1
            conn.execute(
                """
                insert into evidence_chunks(id, evidence_id, repo_full_name, pr_number, chunk_index, chunk_kind,
                  text, token_estimate, is_primary_signal, is_context, is_noise, redaction_hits_json, metadata_json, content_hash, created_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                on conflict(id) do update set
                  text=excluded.text, token_estimate=excluded.token_estimate,
                  is_primary_signal=excluded.is_primary_signal, is_context=excluded.is_context,
                  is_noise=excluded.is_noise, redaction_hits_json=excluded.redaction_hits_json,
                  metadata_json=excluded.metadata_json, content_hash=excluded.content_hash
                """,
                (
                    chunk_id,
                    evidence_id,
                    pr["repo_full_name"],
                    pr["number"],
                    index,
                    item["source_kind"],
                    text,
                    max(1, len(text) // 4),
                    1 if item["is_primary_signal"] else 0,
                    0 if item["is_primary_signal"] else 1,
                    1 if item["is_noise"] else 0,
                    json.dumps(redacted.hits, sort_keys=True),
                    stable_json(metadata),
                    content_hash({"text": text, "index": index, "kind": item["source_kind"]}),
                    utcnow(),
                ),
            )
    return EvidenceStats(created_items, created_chunks, redaction_hits)


def evidence_sources(conn: sqlite3.Connection, pr: sqlite3.Row, *, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    repo = pr["repo_full_name"]
    number = pr["number"]
    sources: list[dict[str, Any]] = []
    events = conn.execute(
        "select * from events where repo_full_name=? and pr_number=? order by created_at, id",
        (repo, number),
    ).fetchall()
    has_pr_body_event = any(event["kind"] == "pr_body" for event in events)
    pr_body = (pr["raw_json"] and json.loads(reveal_text(pr["raw_json"], config) or "{}").get("body")) or ""
    if pr_body.strip() and not has_pr_body_event:
        sources.append(classify_source(pr, {
            "source_table": "prs",
            "source_id": pr["id"],
            "source_kind": "pr_context",
            "actor_login": pr["author_login"],
            "text": f"{pr['title']}\n\n{pr_body}",
            "title": pr["title"],
            "url": pr["url"],
            "created_at": pr["created_at"],
            "updated_at": pr["updated_at"],
            "metadata": {"github_id": pr["github_id"], "review_decision": safe_raw(pr, config=config).get("review_decision")},
        }))
    has_primary = False
    for event in events:
        item = classify_event(pr, event)
        sources.append(item)
        has_primary = has_primary or bool(item["is_primary_signal"])
    files = conn.execute(
        "select * from pr_files where repo_full_name=? and pr_number=? order by path",
        (repo, number),
    ).fetchall()
    for file_row in files:
        raw = json.loads(reveal_text(file_row["raw_json"], config) or "{}")
        patch = raw.get("patch") or ""
        if not patch.strip():
            continue
        source = classify_source(pr, {
            "source_table": "pr_files",
            "source_id": file_row["id"],
            "source_kind": "patch_hunk",
            "actor_login": pr["author_login"],
            "text": patch,
            "title": file_row["path"],
            "path": file_row["path"],
            "created_at": pr["updated_at"],
            "updated_at": pr["updated_at"],
            "metadata": {"status": file_row["status"], "linked_to_primary_signal": has_primary},
        })
        if has_primary or interesting_patch(patch):
            source["signal_score"] = max(float(source["signal_score"]), 0.45)
            sources.append(source)
    return sources


def classify_event(pr: sqlite3.Row, event: sqlite3.Row) -> dict[str, Any]:
    kind = event["kind"]
    check_conclusion = str(event["check_conclusion"] or "").lower()
    source_kind = {
        "review": "review_body",
        "review_comment": "review_comment",
        "issue_comment": "issue_comment",
        "check_run": "failed_check" if check_conclusion in FAILED_CHECK_CONCLUSIONS else "check_run",
        "check_annotation": "check_annotation",
    }.get(kind, kind)
    return classify_source(pr, {
        "source_table": "events",
        "source_id": event["id"],
        "source_kind": source_kind,
        "actor_login": event["actor_login"],
        "text": event["body"] or "",
        "title": event["check_name"] or event["review_state"] or kind,
        "url": event["url"],
        "path": event["path"],
        "line": event["line"],
        "created_at": event["created_at"],
        "updated_at": event["updated_at"],
        "metadata": {
            "event_kind": kind,
            "event_external_id": event["external_id"],
            "review_state": event["review_state"],
            "check_name": event["check_name"],
            "check_conclusion": event["check_conclusion"],
        },
    })


def classify_source(pr: sqlite3.Row, item: dict[str, Any]) -> dict[str, Any]:
    actor = item.get("actor_login") or ""
    text = item.get("text") or ""
    norm = normalize_text(text)
    is_self = bool(actor and actor == pr["author_login"])
    is_bot = actor.endswith("[bot]") or "bot" in actor.lower() or actor in {"github-actions", "dependabot"}
    reviewish = bool(REVIEW_SIGNAL.search(text))
    is_noise = (
        not norm
        or bool(NOISE_ONLY.fullmatch(norm))
        or (is_self and bool(SELF_STATUS.fullmatch(norm)))
        or (is_bot and bool(BOT_NOISE.search(text)) and not reviewish)
    )
    kind = item["source_kind"]
    check_conclusion = str(item["metadata"].get("check_conclusion") or "").lower()
    is_primary = False
    score = 0.1
    if kind in {"review_comment", "review_body"} and not is_self and not is_bot and not is_noise and reviewish:
        is_primary = True
        score = 0.85
    if kind == "review_body" and item["metadata"].get("review_state") == "CHANGES_REQUESTED" and not is_bot:
        is_primary = True
        score = max(score, 0.9)
    if kind == "issue_comment" and not is_self and not is_bot and reviewish:
        is_primary = True
        score = 0.7
    if kind == "issue_comment" and not is_bot and not is_noise and high_signal_context(text):
        is_primary = True
        score = max(score, 0.65 if (is_self or not actor) else 0.75)
    if kind in {"pr_body", "pr_context"} and not is_noise and high_signal_context(text):
        is_primary = True
        score = max(score, 0.6)
    if kind == "failed_check" and (not check_conclusion or check_conclusion in FAILED_CHECK_CONCLUSIONS) and norm:
        is_primary = True
        score = max(score, 0.75)
    if kind == "check_annotation" and (not check_conclusion or check_conclusion in FAILED_CHECK_CONCLUSIONS) and reviewish:
        is_primary = True
        score = max(score, 0.8)
    if is_bot or is_noise:
        is_primary = False
        score = min(score, 0.05)
    item["is_self_authored"] = is_self
    item["is_bot"] = is_bot
    item["is_noise"] = is_noise
    item["is_primary_signal"] = is_primary
    item["signal_score"] = score
    item["actor_role"] = "bot" if is_bot else "author" if is_self else "reviewer" if item.get("actor_login") else "system"
    return item


def interesting_patch(text: str) -> bool:
    return bool(re.search(r"\b(test|throw|null|undefined|auth|secret|token|type|error|validation)\b", text, re.I))


def high_signal_context(text: str) -> bool:
    if not text.strip():
        return False
    if EXPLICIT_SAFETY_CONTEXT.search(text) and (HIGH_SIGNAL_DIRECTIVE.search(text) or TEST_RISK_SIGNAL.search(text) or REVIEW_SIGNAL.search(text)):
        return True
    return bool(SENSITIVE_CONTEXT_SIGNAL.search(text) and HIGH_SIGNAL_DIRECTIVE.search(text))


def stable_evidence_id(pr: sqlite3.Row, item: dict[str, Any]) -> str:
    return "ev_" + slug_key([pr["repo_full_name"], str(pr["number"]), item["source_table"], str(item["source_id"]), item["source_kind"]])


def safe_raw(pr: sqlite3.Row, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        value = json.loads(reveal_text(pr["raw_json"], config) or "{}")
    except json.JSONDecodeError:
        value = {}
    return value if isinstance(value, dict) else {}
