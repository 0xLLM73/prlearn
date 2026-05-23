from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import get_state, set_state
from .crypto import protect_text, reveal_text
from .github import FixtureGitHubClient, GitHubClient
from .util import content_hash, normalize_text, stable_json, subtract_days, utcnow


def sync_prs(conn: sqlite3.Connection, client: GitHubClient, options: dict[str, Any]) -> dict[str, Any]:
    started = utcnow()
    query = {key: value for key, value in options.items() if key != "config"}
    if options.get("incremental") and not options.get("since") and not isinstance(client, FixtureGitHubClient):
        last = get_state(conn, "last_successful_sync_at")
        if last:
            query["since"] = subtract_days(last, int(options.get("lookback_days") or 3))
    run_id = conn.execute(
        "insert into sync_runs(started_at, status, query) values(?, ?, ?)",
        (started, "running", stable_json(query)),
    ).lastrowid
    stats = {"prs_seen": 0, "prs_changed": 0, "events_upserted": 0, "dry_run": bool(options.get("dry_run")), "since": query.get("since")}
    try:
        prs = client.fetch_prs(query)
        stats["prs_seen"] = len(prs)
        if not options.get("dry_run"):
            for pr in prs:
                changed, events = upsert_pr(conn, pr, config=options.get("config"))
                stats["events_upserted"] += events
                if changed:
                    stats["prs_changed"] += 1
            set_state(conn, "last_successful_sync_at", utcnow())
            conn.execute(
                "update sync_runs set finished_at=?, status=?, prs_seen=?, prs_changed=?, events_upserted=? where id=?",
                (utcnow(), "success", stats["prs_seen"], stats["prs_changed"], stats["events_upserted"], run_id),
            )
            conn.commit()
        else:
            conn.execute(
                "update sync_runs set finished_at=?, status=?, prs_seen=? where id=?",
                (utcnow(), "dry_run", stats["prs_seen"], run_id),
            )
            conn.commit()
        stats["run_id"] = run_id
        return stats
    except Exception as exc:
        conn.execute(
            "update sync_runs set finished_at=?, status=?, error=? where id=?",
            (utcnow(), "failed", str(exc), run_id),
        )
        conn.commit()
        raise


def upsert_pr(conn: sqlite3.Connection, pr: dict[str, Any], *, config: dict[str, Any] | None = None) -> tuple[bool, int]:
    repo = pr["repo_full_name"]
    number = int(pr["number"])
    raw = stable_json(pr)
    existing = conn.execute("select raw_json from prs where repo_full_name=? and number=?", (repo, number)).fetchone()
    existing_raw = reveal_text(existing["raw_json"], config) if existing else None
    pr_changed = existing is None or existing_raw != raw
    stored_raw = protect_text(raw, config)
    conn.execute(
        """
        insert into prs(repo_full_name, number, github_id, title, url, author_login, state, is_merged,
          created_at, updated_at, closed_at, merged_at, head_sha, base_ref, head_ref, last_synced_at, dirty, raw_json)
        values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        on conflict(repo_full_name, number) do update set
          github_id=excluded.github_id, title=excluded.title, url=excluded.url, author_login=excluded.author_login,
          state=excluded.state, is_merged=excluded.is_merged, created_at=excluded.created_at, updated_at=excluded.updated_at,
          closed_at=excluded.closed_at, merged_at=excluded.merged_at, head_sha=excluded.head_sha, base_ref=excluded.base_ref,
          head_ref=excluded.head_ref, last_synced_at=excluded.last_synced_at,
          dirty=case when excluded.dirty = 1 then 1 else prs.dirty end,
          raw_json=excluded.raw_json
        """,
        (
            repo,
            number,
            pr.get("github_id"),
            pr.get("title"),
            pr.get("url"),
            pr.get("author_login"),
            pr.get("state"),
            1 if pr.get("is_merged") else 0,
            pr.get("created_at"),
            pr.get("updated_at"),
            pr.get("closed_at"),
            pr.get("merged_at"),
            pr.get("head_sha"),
            pr.get("base_ref"),
            pr.get("head_ref"),
            utcnow(),
            1 if pr_changed else 0,
            stored_raw,
        ),
    )
    events = 0
    for event in normalize_events(pr):
        events += upsert_event(conn, repo, number, event, config=config)
    for item in pr.get("files") or []:
        path = item.get("path") or item.get("filename")
        if not path:
            continue
        conn.execute(
            """
            insert into pr_files(repo_full_name, pr_number, path, status, additions, deletions, changes, language, raw_json)
            values(?,?,?,?,?,?,?,?,?)
            on conflict(repo_full_name, pr_number, path) do update set
              status=excluded.status, additions=excluded.additions, deletions=excluded.deletions,
              changes=excluded.changes, language=excluded.language, raw_json=excluded.raw_json
            """,
            (repo, number, path, item.get("status"), item.get("additions"), item.get("deletions"), item.get("changes"), language_for(path), protect_text(stable_json(item), config)),
        )
    for check in pr.get("check_runs") or []:
        upsert_check(conn, repo, number, check, config=config)
    return pr_changed, events


def normalize_events(pr: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if (pr.get("body") or "").strip():
        events.append({
            "external_id": f"{pr['repo_full_name']}#{pr['number']}:body",
            "kind": "pr_body",
            "actor_login": pr.get("author_login"),
            "body": pr.get("body"),
            "url": pr.get("url"),
            "created_at": pr.get("created_at"),
            "updated_at": pr.get("updated_at"),
            "raw": {
                "body": pr.get("body"),
                "labels": pr.get("labels") or [],
                "review_decision": pr.get("review_decision"),
                "mergeable": pr.get("mergeable"),
            },
        })
    for key, kind in [("reviews", "review"), ("review_comments", "review_comment"), ("issue_comments", "issue_comment"), ("commits", "commit")]:
        for index, item in enumerate(pr.get(key) or []):
            body = item.get("body") or item.get("message") or item.get("title") or ""
            events.append({
                "external_id": str(item.get("id") or item.get("node_id") or f"{pr['repo_full_name']}#{pr['number']}:{kind}:{index}"),
                "kind": kind,
                "actor_login": (item.get("author") or {}).get("login") if isinstance(item.get("author"), dict) else item.get("actor_login"),
                "body": body,
                "path": item.get("path"),
                "line": item.get("line"),
                "commit_sha": item.get("commit_sha") or item.get("oid"),
                "review_state": item.get("state"),
                "url": item.get("url"),
                "created_at": item.get("created_at") or item.get("createdAt") or pr.get("updated_at"),
                "updated_at": item.get("updated_at") or item.get("updatedAt") or item.get("created_at") or pr.get("updated_at"),
                "raw": item,
            })
    for index, item in enumerate(pr.get("timeline") or []):
        event = normalize_timeline_event(pr, item, index)
        if event:
            events.append(event)
    for index, check in enumerate(pr.get("check_runs") or []):
        output = check.get("output") or {}
        body = " ".join(str(value or "") for value in [
            check.get("name"),
            check.get("conclusion"),
            output.get("title"),
            output.get("summary"),
            output.get("text"),
            check.get("details"),
            check.get("summary"),
        ]).strip()
        events.append({
            "external_id": str(check.get("id") or check.get("external_id") or f"{pr['repo_full_name']}#{pr['number']}:check:{index}"),
            "kind": "check_run",
            "body": body,
            "check_name": check.get("name"),
            "check_conclusion": check.get("conclusion"),
            "url": check.get("url") or check.get("link"),
            "created_at": check.get("started_at") or check.get("startedAt") or pr.get("updated_at"),
            "updated_at": check.get("completed_at") or check.get("completedAt") or pr.get("updated_at"),
            "raw": check,
        })
        for ann_index, annotation in enumerate(check.get("annotations") or []):
            events.append(normalize_check_annotation(pr, check, annotation, ann_index))
    return events


def normalize_timeline_event(pr: dict[str, Any], item: dict[str, Any], index: int) -> dict[str, Any] | None:
    event = item.get("event") or item.get("type")
    if not event:
        return None
    if event == "committed":
        return None
    actor = item.get("actor") or item.get("user") or {}
    body_parts = [f"timeline {event}"]
    for key in ["body", "message", "state", "state_reason", "label", "commit_id"]:
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("login") or value.get("sha")
        if value:
            body_parts.append(str(value))
    return {
        "external_id": str(item.get("id") or item.get("node_id") or f"{pr['repo_full_name']}#{pr['number']}:timeline:{index}"),
        "kind": f"timeline_{event}",
        "actor_login": actor.get("login") if isinstance(actor, dict) else None,
        "body": " ".join(body_parts),
        "commit_sha": item.get("commit_id") or item.get("sha"),
        "url": item.get("html_url") or item.get("url"),
        "created_at": item.get("created_at") or pr.get("updated_at"),
        "updated_at": item.get("updated_at") or item.get("created_at") or pr.get("updated_at"),
        "raw": item,
    }


def normalize_check_annotation(pr: dict[str, Any], check: dict[str, Any], annotation: dict[str, Any], index: int) -> dict[str, Any]:
    body = " ".join(str(value or "") for value in [
        check.get("name"),
        check.get("conclusion"),
        annotation.get("annotation_level"),
        annotation.get("title"),
        annotation.get("message"),
        annotation.get("raw_details"),
    ]).strip()
    return {
        "external_id": str(annotation.get("id") or annotation.get("node_id") or f"{check.get('id')}:annotation:{index}"),
        "kind": "check_annotation",
        "body": body,
        "path": annotation.get("path") or annotation.get("blob_href"),
        "line": annotation.get("end_line") or annotation.get("start_line"),
        "check_name": check.get("name"),
        "check_conclusion": check.get("conclusion"),
        "url": annotation.get("html_url") or check.get("html_url") or check.get("url"),
        "created_at": annotation.get("created_at") or check.get("started_at") or check.get("startedAt") or pr.get("updated_at"),
        "updated_at": annotation.get("updated_at") or check.get("completed_at") or check.get("completedAt") or pr.get("updated_at"),
        "raw": annotation,
    }


def upsert_event(conn: sqlite3.Connection, repo: str, number: int, event: dict[str, Any], *, config: dict[str, Any] | None = None) -> int:
    body = event.get("body") or ""
    payload = {**event, "repo_full_name": repo, "pr_number": number}
    digest = content_hash(payload)
    existing = conn.execute("select content_hash from events where external_id=?", (event["external_id"],)).fetchone()
    changed = existing is None or existing["content_hash"] != digest
    conn.execute(
        """
        insert into events(repo_full_name, pr_number, external_id, kind, actor_login, body, normalized_body, path, line,
          commit_sha, review_state, check_name, check_conclusion, url, created_at, updated_at, content_hash, raw_json)
        values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        on conflict(external_id) do update set
          body=excluded.body, normalized_body=excluded.normalized_body, path=excluded.path, line=excluded.line,
          commit_sha=excluded.commit_sha, review_state=excluded.review_state, check_name=excluded.check_name,
          check_conclusion=excluded.check_conclusion, url=excluded.url, updated_at=excluded.updated_at,
          content_hash=excluded.content_hash, raw_json=excluded.raw_json
        """,
        (
            repo,
            number,
            event["external_id"],
            event["kind"],
            event.get("actor_login"),
            body,
            normalize_text(body),
            event.get("path"),
            event.get("line"),
            event.get("commit_sha"),
            event.get("review_state"),
            event.get("check_name"),
            event.get("check_conclusion"),
            event.get("url"),
            event.get("created_at"),
            event.get("updated_at"),
            digest,
            protect_text(stable_json(event.get("raw") or event), config),
        ),
    )
    if changed:
        conn.execute("update prs set dirty=1 where repo_full_name=? and number=?", (repo, number))
        return 1
    return 0


def upsert_check(conn: sqlite3.Connection, repo: str, number: int, check: dict[str, Any], *, config: dict[str, Any] | None = None) -> None:
    external_id = str(check.get("id") or check.get("external_id") or f"{repo}#{number}:check:{check.get('name')}")
    digest = content_hash(check)
    conn.execute(
        """
        insert into check_runs(repo_full_name, pr_number, external_id, name, status, conclusion, started_at, completed_at, url, content_hash, raw_json)
        values(?,?,?,?,?,?,?,?,?,?,?)
        on conflict(external_id) do update set
          name=excluded.name, status=excluded.status, conclusion=excluded.conclusion, started_at=excluded.started_at,
          completed_at=excluded.completed_at, url=excluded.url, content_hash=excluded.content_hash, raw_json=excluded.raw_json
        """,
        (
            repo,
            number,
            external_id,
            check.get("name"),
            check.get("status") or check.get("state"),
            check.get("conclusion"),
            check.get("started_at") or check.get("startedAt"),
            check.get("completed_at") or check.get("completedAt"),
            check.get("url") or check.get("link"),
            digest,
            protect_text(stable_json(check), config),
        ),
    )


def language_for(path: str) -> str | None:
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "go": "go",
        "rs": "rust",
        "rb": "ruby",
        "java": "java",
        "md": "markdown",
    }.get(suffix)
