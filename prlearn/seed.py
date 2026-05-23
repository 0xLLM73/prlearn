from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .chunking import chunks_for_text, stable_chunk_id
from .crypto import protect_text
from .github import GitHubClient, repo_full_name
from .learn import canonical_key, dedupe, extract, upsert_learning_candidate, validate_candidate
from .redaction import redact_text
from .sync import upsert_pr
from .util import content_hash, parse_dt, slug_key, stable_json, subtract_days, utcnow


COMMIT_SIGNAL_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    ("revert", re.compile(r"\brevert(?:ed|ing)?\b", re.I), 0.9),
    ("security", re.compile(r"\b(security|auth(?:entication|orization)?|secret|token|credential|webhook|signature|idempotenc\w*)\b", re.I), 0.86),
    ("regression", re.compile(r"\b(regression|bug|fix(?:e[sd])?|hotfix|crash|failure|failed|failing|broken|typecheck|lint|ci)\b", re.I), 0.78),
]


def seed_account(conn: sqlite3.Connection, client: GitHubClient, options: dict[str, Any]) -> dict[str, Any]:
    now = utcnow()
    lookback_days = int(options.get("lookback_days") or 180)
    since = options.get("since") or subtract_days(now, lookback_days)
    dry_run = bool(options.get("dry_run"))
    mode = str(options.get("mode") or "all")
    max_repos = int(options.get("max_repos") or 25)
    max_prs = int(options.get("max_prs") or 500)
    max_commits_per_repo = int(options.get("max_commits_per_repo") or 50)
    config = options.get("config")

    stats: dict[str, Any] = {
        "dry_run": dry_run,
        "since": since,
        "repos_seen": 0,
        "repos_selected": 0,
        "repos_skipped": 0,
        "selected_repos": [],
        "prs_seen": 0,
        "prs_changed": 0,
        "events_upserted": 0,
        "commits_seen": 0,
        "commit_signals": 0,
        "commit_candidates_created": 0,
        "commit_candidates_updated": 0,
    }

    repos = client.list_repositories(
        {
            "repos": options.get("repos") or [],
            "owners": options.get("owners") or [],
            "repo_limit": options.get("repo_limit") or max(max_repos * 4, 100),
        }
    )
    stats["repos_seen"] = len(repos)
    ranked = [rank_repo(repo, since=since, options=options) for repo in repos]
    ranked.sort(key=lambda item: (item["skipped_reason"] is None, item["score"], item["repo_full_name"]), reverse=True)
    selected = [item for item in ranked if item["skipped_reason"] is None][:max_repos]
    stats["repos_selected"] = len(selected)
    stats["repos_skipped"] = len(ranked) - len(selected)
    stats["selected_repos"] = [item["repo_full_name"] for item in selected]

    if not dry_run:
        for item in ranked:
            upsert_seed_repo(conn, item, now=now)
        conn.commit()

    if mode in {"all", "prs"} and selected:
        pr_stats = seed_prs(conn, client, selected, since=since, options={**options, "limit": max_prs}, dry_run=dry_run, config=config)
        stats.update(pr_stats)
        if not dry_run and (stats["prs_changed"] or stats["events_upserted"]):
            engine = str(options.get("engine") or "heuristic")
            extract_stats = extract(
                conn,
                engine=engine,
                config=config,
                allow_context_only=bool(options.get("allow_context_only")),
                model_limit=options.get("max_model_prs"),
            )
            stats["extract"] = extract_stats
            stats["dedupe"] = dedupe(conn)

    if mode in {"all", "commits"} and selected:
        commit_stats = seed_commits(
            conn,
            client,
            selected,
            since=since,
            author=options.get("author"),
            limit=max_commits_per_repo,
            dry_run=dry_run,
            config=config,
        )
        for key, value in commit_stats.items():
            stats[key] = value

    if not dry_run:
        conn.commit()
    return stats


def seed_prs(
    conn: sqlite3.Connection,
    client: GitHubClient,
    selected: list[dict[str, Any]],
    *,
    since: str,
    options: dict[str, Any],
    dry_run: bool,
    config: dict[str, Any] | None,
) -> dict[str, int]:
    query = {
        "repos": [item["repo_full_name"] for item in selected],
        "author": options.get("author"),
        "since": since,
        "limit": options.get("limit"),
    }
    prs = client.fetch_prs(query)
    if dry_run:
        return {"prs_seen": len(prs), "prs_changed": 0, "events_upserted": 0}
    changed = 0
    events = 0
    latest_by_repo: dict[str, str] = {}
    for pr in prs:
        pr_changed, pr_events = upsert_pr(conn, pr, config=config)
        changed += 1 if pr_changed else 0
        events += pr_events
        repo = pr.get("repo_full_name")
        updated_at = str(pr.get("updated_at") or "")
        if repo and updated_at > latest_by_repo.get(repo, ""):
            latest_by_repo[repo] = updated_at
    for repo, updated_at in latest_by_repo.items():
        conn.execute("update seed_repos set last_seeded_pr_at=? where repo_full_name=?", (updated_at, repo))
    conn.commit()
    return {"prs_seen": len(prs), "prs_changed": changed, "events_upserted": events}


def seed_commits(
    conn: sqlite3.Connection,
    client: GitHubClient,
    selected: list[dict[str, Any]],
    *,
    since: str,
    author: str | None,
    limit: int,
    dry_run: bool,
    config: dict[str, Any] | None,
) -> dict[str, int]:
    stats = {"commits_seen": 0, "commit_signals": 0, "commit_candidates_created": 0, "commit_candidates_updated": 0}
    for repo in selected:
        repo_name = str(repo["repo_full_name"])
        commits = client.fetch_commits(repo_name, {"author": author, "since": since, "limit": limit})
        stats["commits_seen"] += len(commits)
        latest_commit = latest_commit_cursor(commits)
        if not dry_run:
            ensure_commit_seed_pr(conn, repo_name, author=author, config=config)
        for commit in commits:
            signal = classify_commit_signal(str(commit.get("message") or ""))
            if not signal:
                continue
            stats["commit_signals"] += 1
            if dry_run:
                continue
            signal_id = upsert_commit_signal(conn, repo_name, commit, signal, config=config)
            evidence = ensure_commit_signal_evidence(conn, repo_name, signal_id, commit, signal)
            candidate = candidate_for_commit_signal(signal, evidence, str(commit.get("message") or ""))
            validation = validate_candidate(conn, candidate, secret_echo=False, allow_context_only=True)
            if validation["errors"]:
                continue
            created = upsert_learning_candidate(conn, None, candidate, validation, source="seed_commit")  # type: ignore[arg-type]
            key = canonical_key(str(candidate["title"]), str(candidate["prevention_rule"]), tuple(str(tag) for tag in candidate["tags"]))
            candidate_id = "cand_" + slug_key(["seed_commit", key])
            conn.execute("update seed_commit_signals set candidate_id=?, updated_at=? where id=?", (candidate_id, utcnow(), signal_id))
            if created:
                stats["commit_candidates_created"] += 1
            else:
                stats["commit_candidates_updated"] += 1
        if latest_commit and not dry_run:
            conn.execute(
                "update seed_repos set last_seeded_commit_sha=?, last_seeded_commit_at=? where repo_full_name=?",
                (latest_commit["sha"], latest_commit["committed_at"], repo_name),
            )
    if not dry_run:
        conn.commit()
    return stats


def rank_repo(repo: dict[str, Any], *, since: str, options: dict[str, Any]) -> dict[str, Any]:
    full_name = repo_full_name(repo)
    owner, name = full_name.split("/", 1) if "/" in full_name else ("", full_name)
    pushed_at = str(repo.get("pushed_at") or repo.get("pushedAt") or repo.get("updated_at") or repo.get("updatedAt") or "")
    updated_at = str(repo.get("updated_at") or repo.get("updatedAt") or pushed_at)
    archived = bool(repo.get("archived") or repo.get("isArchived"))
    fork = bool(repo.get("fork") or repo.get("isFork"))
    requested_repos = set(options.get("repos") or [])
    include_archived = bool(options.get("include_archived"))
    include_forks = bool(options.get("include_forks"))
    include_inactive = bool(options.get("include_inactive"))
    skipped_reason = None
    if archived and not include_archived:
        skipped_reason = "archived"
    elif fork and not include_forks:
        skipped_reason = "fork"
    elif pushed_at and pushed_at < since and full_name not in requested_repos and not include_inactive:
        skipped_reason = "inactive"

    score = repo_score(repo, since=since)
    return {
        "repo_full_name": full_name,
        "owner": owner,
        "name": name,
        "default_branch": default_branch(repo),
        "private": bool(repo.get("private") or repo.get("isPrivate")),
        "fork": fork,
        "archived": archived,
        "language": language(repo),
        "size": int(repo.get("size") or 0),
        "open_issues_count": int(repo.get("open_issues_count") or repo.get("openIssuesCount") or 0),
        "pushed_at": pushed_at or None,
        "updated_at": updated_at or None,
        "score": score,
        "skipped_reason": skipped_reason,
        "raw_json": stable_json(repo),
    }


def repo_score(repo: dict[str, Any], *, since: str) -> float:
    pushed_at = str(repo.get("pushed_at") or repo.get("pushedAt") or "")
    updated_at = str(repo.get("updated_at") or repo.get("updatedAt") or pushed_at)
    score = 0.0
    if pushed_at >= since:
        score += 50.0
    if updated_at >= since:
        score += 20.0
    score += min(float(repo.get("open_issues_count") or repo.get("openIssuesCount") or 0), 25.0) * 0.2
    score += min(float(repo.get("size") or 0), 5000.0) / 1000.0
    pushed = parse_dt(pushed_at)
    if pushed:
        age_days = max(0, (datetime.now(UTC) - pushed).days)
        score += max(0.0, 30.0 - min(age_days, 365) / 12.0)
    return round(score, 3)


def upsert_seed_repo(conn: sqlite3.Connection, item: dict[str, Any], *, now: str) -> None:
    conn.execute(
        """
        insert into seed_repos(repo_full_name, owner, name, default_branch, private, fork, archived, language, size,
          open_issues_count, pushed_at, updated_at, score, skipped_reason, last_seen_at, raw_json)
        values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        on conflict(repo_full_name) do update set
          owner=excluded.owner, name=excluded.name, default_branch=excluded.default_branch,
          private=excluded.private, fork=excluded.fork, archived=excluded.archived, language=excluded.language,
          size=excluded.size, open_issues_count=excluded.open_issues_count, pushed_at=excluded.pushed_at,
          updated_at=excluded.updated_at, score=excluded.score, skipped_reason=excluded.skipped_reason,
          last_seen_at=excluded.last_seen_at, raw_json=excluded.raw_json
        """,
        (
            item["repo_full_name"],
            item["owner"],
            item["name"],
            item["default_branch"],
            1 if item["private"] else 0,
            1 if item["fork"] else 0,
            1 if item["archived"] else 0,
            item["language"],
            item["size"],
            item["open_issues_count"],
            item["pushed_at"],
            item["updated_at"],
            item["score"],
            item["skipped_reason"],
            now,
            item["raw_json"],
        ),
    )


def ensure_commit_seed_pr(conn: sqlite3.Connection, repo: str, *, author: str | None, config: dict[str, Any] | None) -> None:
    now = utcnow()
    raw = {"source": "seed_commit_history", "repo_full_name": repo, "number": 0}
    conn.execute(
        """
        insert into prs(repo_full_name, number, github_id, title, url, author_login, state, is_merged,
          created_at, updated_at, closed_at, merged_at, head_sha, base_ref, head_ref, last_synced_at, dirty, raw_json)
        values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        on conflict(repo_full_name, number) do update set
          title=excluded.title, updated_at=excluded.updated_at, last_synced_at=excluded.last_synced_at,
          raw_json=excluded.raw_json
        """,
        (
            repo,
            0,
            f"seed:{repo}:commits",
            "Commit history seed",
            f"https://github.com/{repo}",
            str(author or "").lstrip("@") or None,
            "SEED",
            1,
            now,
            now,
            now,
            now,
            None,
            None,
            None,
            now,
            0,
            protect_text(stable_json(raw), config),
        ),
    )


def upsert_commit_signal(conn: sqlite3.Connection, repo: str, commit: dict[str, Any], signal: dict[str, Any], *, config: dict[str, Any] | None) -> str:
    now = utcnow()
    sha = str(commit.get("sha") or "")
    signal_id = "scs_" + slug_key([repo, sha])
    redacted_message = redact_text(str(commit.get("message") or "")).text
    conn.execute(
        """
        insert into seed_commit_signals(id, repo_full_name, sha, message, author_login, committed_at, url, signal, score, raw_json, created_at, updated_at)
        values(?,?,?,?,?,?,?,?,?,?,?,?)
        on conflict(repo_full_name, sha) do update set
          message=excluded.message, author_login=excluded.author_login, committed_at=excluded.committed_at,
          url=excluded.url, signal=excluded.signal, score=excluded.score, raw_json=excluded.raw_json,
          updated_at=excluded.updated_at
        """,
        (
            signal_id,
            repo,
            sha,
            redacted_message,
            commit.get("author_login"),
            commit.get("committed_at"),
            commit.get("url"),
            signal["kind"],
            signal["score"],
            protect_text(stable_json(commit.get("raw") or commit), config),
            now,
            now,
        ),
    )
    return signal_id


def ensure_commit_signal_evidence(conn: sqlite3.Connection, repo: str, signal_id: str, commit: dict[str, Any], signal: dict[str, Any]) -> dict[str, Any]:
    message = str(commit.get("message") or "").strip()
    redacted = redact_text(message)
    now = utcnow()
    evidence_id = "ev_" + slug_key([repo, "0", "seed_commit_signals", signal_id, "commit_signal"])
    metadata = {"sha": commit.get("sha"), "signal": signal["kind"], "committed_at": commit.get("committed_at")}
    conn.execute(
        """
        insert into evidence_items(id, repo_full_name, pr_number, source_table, source_id, source_kind,
          actor_login, actor_role, is_primary_signal, is_self_authored, is_bot, is_noise, signal_score,
          title, url, path, line, created_at, updated_at, redacted_text, redaction_hits_json, metadata_json, content_hash)
        values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        on conflict(id) do update set
          actor_login=excluded.actor_login, signal_score=excluded.signal_score, title=excluded.title,
          url=excluded.url, updated_at=excluded.updated_at, redacted_text=excluded.redacted_text,
          redaction_hits_json=excluded.redaction_hits_json, metadata_json=excluded.metadata_json,
          content_hash=excluded.content_hash
        """,
        (
            evidence_id,
            repo,
            0,
            "seed_commit_signals",
            signal_id,
            "commit_signal",
            commit.get("author_login"),
            "author",
            1,
            1,
            0,
            0,
            signal["score"],
            signal["title"],
            commit.get("url"),
            None,
            None,
            commit.get("committed_at") or now,
            now,
            redacted.text,
            json.dumps(redacted.hits, sort_keys=True),
            stable_json(metadata),
            content_hash({"text": redacted.text, "metadata": metadata}),
        ),
    )
    chunk_ids = []
    for index, text in enumerate(chunks_for_text(redacted.text, kind="commit_signal")):
        chunk_id = stable_chunk_id(evidence_id, index, text)
        chunk_ids.append(chunk_id)
        conn.execute(
            """
            insert into evidence_chunks(id, evidence_id, repo_full_name, pr_number, chunk_index, chunk_kind,
              text, token_estimate, is_primary_signal, is_context, is_noise, redaction_hits_json, metadata_json, content_hash, created_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            on conflict(id) do update set
              text=excluded.text, token_estimate=excluded.token_estimate, is_primary_signal=excluded.is_primary_signal,
              is_context=excluded.is_context, is_noise=excluded.is_noise, redaction_hits_json=excluded.redaction_hits_json,
              metadata_json=excluded.metadata_json, content_hash=excluded.content_hash
            """,
            (
                chunk_id,
                evidence_id,
                repo,
                0,
                index,
                "commit_signal",
                text,
                max(1, len(text) // 4),
                1,
                0,
                0,
                json.dumps(redacted.hits, sort_keys=True),
                stable_json(metadata),
                content_hash({"text": text, "index": index, "kind": "commit_signal"}),
                now,
            ),
        )
    return {"evidence_id": evidence_id, "chunk_ids": chunk_ids, "quote": redacted.text[:500]}


def classify_commit_signal(message: str) -> dict[str, Any] | None:
    first_line = message.strip().splitlines()[0] if message.strip() else ""
    for kind, pattern, score in COMMIT_SIGNAL_PATTERNS:
        if pattern.search(first_line):
            return {"kind": kind, "score": score, "title": commit_signal_title(kind)}
    return None


def candidate_for_commit_signal(signal: dict[str, Any], evidence: dict[str, Any], message: str) -> dict[str, Any]:
    kind = signal["kind"]
    if kind == "revert":
        title = "Treat revert commits as regression evidence"
        lesson = "A revert commit indicates the previous change shipped with a failed assumption that should be captured before similar work is attempted again."
        prevention = "When a change is reverted, capture the failed assumption and add a regression check before reattempting related work."
        tags = ["regression", "process", "commits"]
        severity = "medium"
        learning_type = "process"
    elif kind == "security":
        title = "Review security fix commits for explicit guardrails"
        lesson = "Commits touching security, auth, webhook, or secret-handling behavior are strong evidence that the project needs explicit negative checks around those paths."
        prevention = "When a commit touches security, auth, webhook, or secret-handling behavior, verify the guardrail and add a negative test before shipping."
        tags = ["security", "auth", "tests"]
        severity = "high"
        learning_type = "testing"
    else:
        title = "Turn fix commits into regression tests"
        lesson = "Fix, bug, crash, and CI repair commits identify behavior that should be protected by a repeatable check."
        prevention = "When a commit message indicates a fix, bug, crash, failure, or regression, add or update a regression test that would have failed before the fix."
        tags = ["tests", "regression", "ci"]
        severity = "medium"
        learning_type = "testing"
    return {
        "title": title,
        "lesson": lesson,
        "mistake_pattern": f"commit-signal-{kind}",
        "prevention_rule": prevention,
        "tags": tags,
        "severity": severity,
        "confidence": float(signal["score"]),
        "learning_type": learning_type,
        "evidence_ids": [evidence["evidence_id"]],
        "evidence_chunk_ids": evidence["chunk_ids"][:1],
        "evidence_quotes": [evidence["quote"] or message.strip()[:500]],
    }


def commit_signal_title(kind: str) -> str:
    return {
        "revert": "Revert commit",
        "security": "Security-sensitive commit",
        "regression": "Fix or regression commit",
    }.get(kind, "Commit signal")


def latest_commit_cursor(commits: list[dict[str, Any]]) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for commit in commits:
        committed_at = str(commit.get("committed_at") or "")
        if not latest or committed_at > str(latest.get("committed_at") or ""):
            latest = commit
    return latest


def default_branch(repo: dict[str, Any]) -> str | None:
    branch = repo.get("default_branch")
    if branch:
        return str(branch)
    ref = repo.get("defaultBranchRef")
    if isinstance(ref, dict):
        return str(ref.get("name") or "") or None
    return None


def language(repo: dict[str, Any]) -> str | None:
    value = repo.get("language")
    if value:
        return str(value)
    primary = repo.get("primaryLanguage")
    if isinstance(primary, dict):
        return str(primary.get("name") or "") or None
    return None
