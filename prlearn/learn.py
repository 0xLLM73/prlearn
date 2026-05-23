from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from .codex_client import CodexError, client_from_config as codex_client_from_config
from .config import codex_config, ollama_config, openai_config
from .evidence import ensure_evidence_for_dirty_prs, select_prs
from .ollama_client import OllamaError, OllamaModelMissing, client_from_config
from .openai_client import OpenAIError, client_from_config as openai_client_from_config
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, SYSTEM_PROMPT_UNREDACTED, extraction_user_prompt
from .redaction import contains_secret, redact_identifier, redact_text
from .schemas import EXTRACTION_SCHEMA, schema_hash
from .util import normalize_text, slug_key, utcnow

NOISE_ONLY = re.compile(r"^(lgtm|nice work|looks good|great job|thanks|thank you|nit)$", re.I)
SEVERITY = {"low": 1, "medium": 3, "high": 4, "critical": 5}


class ExtractionEngineError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    title: str
    lesson: str
    prevention_rule: str
    mistake_pattern: str
    tags: tuple[str, ...]
    severity: int
    confidence: float
    event_id: int
    repo_full_name: str
    pr_number: int
    quote: str
    url: str | None

    @property
    def canonical_key(self) -> str:
        return canonical_key(self.title, self.prevention_rule, self.tags)


def canonical_key(title: str, prevention_rule: str, tags: Iterable[str]) -> str:
    return slug_key([title, prevention_rule, ",".join(sorted(tags))])


RULES = [
    {
        "name": "tests-null-state",
        "patterns": ["missing test", "add test", "regression test", "edge case", "null", "undefined", "empty state", "error state"],
        "requires_any": [["test"], ["null", "undefined", "empty state", "error state", "edge case"]],
        "title": "Cover null and empty states with regression tests",
        "lesson": "Review feedback repeatedly caught missing coverage for null, undefined, empty, or edge-case UI/data states.",
        "prevention": "Before opening a PR, add or update regression tests for null, undefined, empty, and error states touched by the change.",
        "tags": ("tests", "edge-cases", "ui"),
        "severity": 3,
        "confidence": 0.82,
    },
    {
        "name": "webhook-signature",
        "patterns": ["signature verification", "webhook", "auth", "authorization", "security", "idempotency"],
        "requires_any": [["webhook"], ["signature", "auth", "authorization", "security", "idempotency"]],
        "title": "Verify webhook signatures before trusting payloads",
        "lesson": "Webhook handlers must authenticate payloads and guard against replay or duplicate delivery before processing.",
        "prevention": "For every webhook handler, add signature verification, replay/idempotency protection, and a negative test for invalid signatures.",
        "tags": ("security", "webhook", "api", "auth"),
        "severity": 5,
        "confidence": 0.9,
    },
    {
        "name": "typecheck-before-pr",
        "patterns": ["typecheck", "lint", "build failed", "ci failed", "this will throw", "this can break"],
        "requires_any": [["typecheck", "lint", "build", "ci"], ["failed", "failure", "throw", "break", "error"]],
        "title": "Run typecheck and build checks before opening PRs",
        "lesson": "CI/check feedback found failures that local validation should catch before review.",
        "prevention": "Run the project's typecheck, lint, and build commands before opening a PR or after accepting generated code.",
        "tags": ("types", "ci", "validation"),
        "severity": 4,
        "confidence": 0.86,
    },
    {
        "name": "input-validation",
        "patterns": ["validation", "should handle", "please handle", "changes requested"],
        "requires_any": [["validation", "should handle", "please handle", "changes requested"]],
        "title": "Handle validation and requested edge cases explicitly",
        "lesson": "Review feedback asked for explicit handling of validation or edge-case behavior before merge.",
        "prevention": "When a change adds user input or branchy behavior, validate bad inputs and add tests for requested edge cases.",
        "tags": ("validation", "edge-cases"),
        "severity": 3,
        "confidence": 0.65,
    },
    {
        "name": "race-performance",
        "patterns": ["race condition", "performance"],
        "requires_any": [["race condition", "performance"]],
        "title": "Check concurrency and performance risks in changed paths",
        "lesson": "Review feedback flagged runtime behavior that can degrade under concurrency or load.",
        "prevention": "For stateful or hot-path changes, reason about concurrent calls, add guardrails, and include performance-sensitive tests where practical.",
        "tags": ("performance", "concurrency"),
        "severity": 4,
        "confidence": 0.7,
    },
]


def extract(
    conn: sqlite3.Connection,
    *,
    all_prs: bool = False,
    pr_ref: str | None = None,
    min_confidence: float = 0.55,
    engine: str = "heuristic",
    config: dict[str, Any] | None = None,
    allow_context_only: bool = False,
    model_limit: int | None = None,
) -> dict[str, Any]:
    if engine not in {"heuristic", "ollama", "hybrid", "codex", "openai"}:
        raise ExtractionEngineError(f"unsupported extraction engine: {engine}")
    if engine == "heuristic":
        stats = extract_heuristic(conn, all_prs=all_prs, pr_ref=pr_ref, min_confidence=min_confidence)
        return {"engine": engine, **stats}
    if engine == "hybrid":
        ollama_stats = extract_ollama_candidates(
            conn,
            all_prs=all_prs,
            pr_ref=pr_ref,
            config=config,
            strict=False,
            clear_dirty=False,
            allow_context_only=allow_context_only,
            model_limit=model_limit,
        )
        heuristic_stats = extract_heuristic(conn, all_prs=all_prs, pr_ref=pr_ref, min_confidence=min_confidence)
        return {"engine": engine, **heuristic_stats, **ollama_stats}
    if engine == "codex":
        stats = extract_codex_candidates(
            conn,
            all_prs=all_prs,
            pr_ref=pr_ref,
            config=config,
            clear_dirty=True,
            allow_context_only=allow_context_only,
            model_limit=model_limit,
        )
        return {
            "engine": engine,
            "prs_processed": stats.get("codex_prs_processed", 0),
            "created": 0,
            "updated": 0,
            "candidates": stats.get("candidates_created", 0),
            **stats,
        }
    if engine == "openai":
        stats = extract_openai_candidates(
            conn,
            all_prs=all_prs,
            pr_ref=pr_ref,
            config=config,
            clear_dirty=True,
            allow_context_only=allow_context_only,
            model_limit=model_limit,
        )
        return {
            "engine": engine,
            "prs_processed": stats.get("openai_prs_processed", 0),
            "created": 0,
            "updated": 0,
            "candidates": stats.get("candidates_created", 0),
            **stats,
        }
    stats = extract_ollama_candidates(
        conn,
        all_prs=all_prs,
        pr_ref=pr_ref,
        config=config,
        strict=True,
        clear_dirty=True,
        allow_context_only=allow_context_only,
        model_limit=model_limit,
    )
    return {
        "engine": engine,
        "prs_processed": stats.get("ollama_prs_processed", 0),
        "created": 0,
        "updated": 0,
        "candidates": stats.get("candidates_created", 0),
        **stats,
    }


def extract_heuristic(conn: sqlite3.Connection, *, all_prs: bool = False, pr_ref: str | None = None, min_confidence: float = 0.55) -> dict[str, int]:
    where = []
    params: list[object] = []
    if not all_prs:
        where.append("p.dirty = 1")
    if pr_ref:
        repo, number = parse_pr_ref(pr_ref)
        where.append("p.repo_full_name = ? and p.number = ?")
        params.extend([repo, number])
    clause = " where " + " and ".join(where) if where else ""
    prs = conn.execute(f"select p.* from prs p{clause}", params).fetchall()
    created = updated = candidates_count = 0
    for pr in prs:
        events = conn.execute(
            "select * from events where repo_full_name=? and pr_number=? order by created_at, id",
            (pr["repo_full_name"], pr["number"]),
        ).fetchall()
        for event in events:
            for candidate in candidates_for_event(event):
                if candidate.confidence < min_confidence:
                    continue
                candidates_count += 1
                was_created = upsert_learning(conn, candidate)
                if was_created:
                    created += 1
                else:
                    updated += 1
        conn.execute("update prs set dirty=0 where id=?", (pr["id"],))
    conn.commit()
    return {"prs_processed": len(prs), "candidates": candidates_count, "created": created, "updated": updated}


def candidates_for_event(event: sqlite3.Row) -> list[Candidate]:
    if event["kind"] not in {"review", "review_comment", "issue_comment", "check_run", "check_annotation"}:
        return []
    body = event["body"] or ""
    norm = normalize_text(body)
    if not norm or NOISE_ONLY.fullmatch(norm):
        return []
    candidates: list[Candidate] = []
    for rule in RULES:
        if rule["name"] == "input-validation" and candidates:
            continue
        if not any(pattern in norm for pattern in rule["patterns"]):
            continue
        if not requirement_matches(norm, rule["requires_any"]):
            continue
        candidates.append(
            Candidate(
                title=rule["title"],
                lesson=rule["lesson"],
                prevention_rule=rule["prevention"],
                mistake_pattern=rule["name"],
                tags=tuple(rule["tags"]),
                severity=int(rule["severity"]),
                confidence=float(rule["confidence"]),
                event_id=int(event["id"]),
                repo_full_name=event["repo_full_name"],
                pr_number=int(event["pr_number"]),
                quote=body.strip()[:500],
                url=event["url"],
            )
        )
    return candidates


def requirement_matches(norm: str, groups: list[list[str]]) -> bool:
    return all(any(term in norm for term in group) for group in groups)


def upsert_learning(conn: sqlite3.Connection, candidate: Candidate) -> bool:
    now = utcnow()
    row = conn.execute("select * from learning_cards where canonical_key=?", (candidate.canonical_key,)).fetchone()
    if row and row["status"] == "rejected":
        return False
    if not row:
        learning_id = conn.execute(
            """
            insert into learning_cards(canonical_key, title, lesson, mistake_pattern, prevention_rule, tags_json,
              contexts_json, severity, confidence, status, recurrence_count, first_seen_at, last_seen_at, created_at, updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                candidate.canonical_key,
                candidate.title,
                candidate.lesson,
                candidate.mistake_pattern,
                candidate.prevention_rule,
                json.dumps(list(candidate.tags)),
                json.dumps([candidate.repo_full_name]),
                candidate.severity,
                candidate.confidence,
                "pending",
                1,
                now,
                now,
                now,
                now,
            ),
        ).lastrowid
        attach_evidence(conn, int(learning_id), candidate)
        return True
    learning_id = int(row["id"])
    before = evidence_count(conn, learning_id)
    attach_evidence(conn, learning_id, candidate)
    after = evidence_count(conn, learning_id)
    if after > before:
        contexts = sorted(set(json.loads(row["contexts_json"]) + [candidate.repo_full_name]))
        confidence = max(float(row["confidence"]), candidate.confidence)
        severity = max(int(row["severity"]), candidate.severity)
        conn.execute(
            """
            update learning_cards set recurrence_count=?, last_seen_at=?, updated_at=?, contexts_json=?,
              confidence=?, severity=? where id=?
            """,
            (after, now, now, json.dumps(contexts), confidence, severity, learning_id),
        )
    return False


def attach_evidence(conn: sqlite3.Connection, learning_id: int, candidate: Candidate) -> None:
    conn.execute(
        """
        insert or ignore into learning_evidence(learning_id, event_id, repo_full_name, pr_number, quote, url, created_at)
        values(?,?,?,?,?,?,?)
        """,
        (
            learning_id,
            candidate.event_id,
            candidate.repo_full_name,
            candidate.pr_number,
            redact_text(candidate.quote).text,
            candidate.url,
            utcnow(),
        ),
    )


def evidence_count(conn: sqlite3.Connection, learning_id: int) -> int:
    return int(conn.execute("select count(*) as count from learning_evidence where learning_id=?", (learning_id,)).fetchone()["count"])


def extract_ollama_candidates(
    conn: sqlite3.Connection,
    *,
    all_prs: bool = False,
    pr_ref: str | None = None,
    config: dict[str, Any] | None = None,
    strict: bool,
    clear_dirty: bool,
    allow_context_only: bool = False,
    model_limit: int | None = None,
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "ollama_prs_processed": 0,
        "ollama_skipped": 0,
        "model_runs_created": 0,
        "model_runs_reused": 0,
        "model_runs_failed": 0,
        "candidates_created": 0,
        "candidates_rejected": 0,
        "no_learning_prs": 0,
        "context_only_prs_processed": 0,
    }
    ollama = ollama_config(config or {})
    model = str((ollama.get("models") or {}).get("extractor") or "qwen3.5:9b")
    options = dict(ollama.get("options") or {})
    try:
        client = client_from_config(ollama, timeout=float(ollama.get("request_timeout_seconds") or 180))
        client.version()
        installed = client.installed_models()
        if model not in installed:
            raise OllamaModelMissing(f"Ollama model not installed: {model}. Run `ollama pull {model}`.")
    except OllamaError as exc:
        if strict:
            raise ExtractionEngineError(str(exc)) from exc
        stats["ollama_skipped"] = 1
        stats["ollama_error"] = str(exc)
        return stats

    stats.update(ensure_evidence_for_dirty_prs(conn, all_prs=all_prs, pr_ref=pr_ref, config=config))
    prs = limited_prs(select_prs(conn, all_prs=all_prs, pr_ref=pr_ref), model_limit)
    for pr in prs:
        chunks = chunks_for_pr(conn, pr["repo_full_name"], int(pr["number"]))
        has_primary = any(int(chunk["is_primary_signal"]) for chunk in chunks)
        if not chunks or (not has_primary and not allow_context_only):
            stats["no_learning_prs"] += 1
            if clear_dirty:
                conn.execute("update prs set dirty=0 where id=?", (pr["id"],))
                conn.commit()
            continue
        if not has_primary:
            stats["context_only_prs_processed"] += 1
        run_result = run_model_extraction(
            conn,
            client,
            source="ollama",
            model=model,
            options=options,
            payload=extraction_payload(pr, chunks, redact_metadata=True),
            redacted_prompt=True,
        )
        if run_result["cache_hit"]:
            stats["model_runs_reused"] += 1
        elif run_result["status"] == "success":
            stats["model_runs_created"] += 1
        else:
            stats["model_runs_failed"] += 1
        if run_result["status"] != "success":
            conn.commit()
            if strict and run_result.get("fatal"):
                raise ExtractionEngineError(str(run_result.get("error") or "Ollama extraction failed"))
            continue
        stored = store_model_candidates(
            conn,
            run_result["model_run_id"],
            run_result["data"],
            source="ollama",
            secret_echo=bool(run_result.get("secret_echo")),
            allow_context_only=allow_context_only,
        )
        for key, value in stored.items():
            stats[key] = int(stats.get(key, 0)) + int(value)
        stats["ollama_prs_processed"] += 1
        if clear_dirty:
            conn.execute("update prs set dirty=0 where id=?", (pr["id"],))
        conn.commit()
    conn.commit()
    return stats


def extract_codex_candidates(
    conn: sqlite3.Connection,
    *,
    all_prs: bool = False,
    pr_ref: str | None = None,
    config: dict[str, Any] | None = None,
    clear_dirty: bool,
    allow_context_only: bool = False,
    model_limit: int | None = None,
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "codex_prs_processed": 0,
        "codex_skipped": 0,
        "model_runs_created": 0,
        "model_runs_reused": 0,
        "model_runs_failed": 0,
        "candidates_created": 0,
        "candidates_rejected": 0,
        "no_learning_prs": 0,
        "context_only_prs_processed": 0,
    }
    codex = codex_config(config or {})
    model = str(codex.get("model") or codex.get("profile") or "codex-cli-default")
    options = {
        key: value
        for key, value in codex.items()
        if key in {"command", "request_timeout_seconds", "model", "reasoning_effort", "profile", "extra_args", "include_raw_metadata"}
    }
    try:
        client = codex_client_from_config(codex)
        client.version()
        model = client.model_label
    except CodexError as exc:
        raise ExtractionEngineError(str(exc)) from exc

    stats.update(ensure_evidence_for_dirty_prs(conn, all_prs=all_prs, pr_ref=pr_ref, config=config))
    prs = limited_prs(select_prs(conn, all_prs=all_prs, pr_ref=pr_ref), model_limit)
    redact_metadata = not bool(codex.get("include_raw_metadata", True))
    for pr in prs:
        chunks = chunks_for_pr(conn, pr["repo_full_name"], int(pr["number"]))
        has_primary = any(int(chunk["is_primary_signal"]) for chunk in chunks)
        if not chunks or (not has_primary and not allow_context_only):
            stats["no_learning_prs"] += 1
            if clear_dirty:
                conn.execute("update prs set dirty=0 where id=?", (pr["id"],))
                conn.commit()
            continue
        if not has_primary:
            stats["context_only_prs_processed"] += 1
        run_result = run_model_extraction(
            conn,
            client,
            source="codex",
            model=model,
            options=options,
            payload=extraction_payload(pr, chunks, redact_metadata=redact_metadata),
            redacted_prompt=redact_metadata,
        )
        if run_result["cache_hit"]:
            stats["model_runs_reused"] += 1
        elif run_result["status"] == "success":
            stats["model_runs_created"] += 1
        else:
            stats["model_runs_failed"] += 1
        if run_result["status"] != "success":
            conn.commit()
            if run_result.get("fatal"):
                raise ExtractionEngineError(str(run_result.get("error") or "Codex extraction failed"))
            continue
        stored = store_model_candidates(
            conn,
            run_result["model_run_id"],
            run_result["data"],
            source="codex",
            secret_echo=bool(run_result.get("secret_echo")),
            allow_context_only=allow_context_only,
        )
        for key, value in stored.items():
            stats[key] = int(stats.get(key, 0)) + int(value)
        stats["codex_prs_processed"] += 1
        if clear_dirty:
            conn.execute("update prs set dirty=0 where id=?", (pr["id"],))
        conn.commit()
    conn.commit()
    return stats


def extract_openai_candidates(
    conn: sqlite3.Connection,
    *,
    all_prs: bool = False,
    pr_ref: str | None = None,
    config: dict[str, Any] | None = None,
    clear_dirty: bool,
    allow_context_only: bool = False,
    model_limit: int | None = None,
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "openai_prs_processed": 0,
        "openai_skipped": 0,
        "model_runs_created": 0,
        "model_runs_reused": 0,
        "model_runs_failed": 0,
        "candidates_created": 0,
        "candidates_rejected": 0,
        "no_learning_prs": 0,
        "context_only_prs_processed": 0,
    }
    openai = openai_config(config or {})
    model = str(openai.get("model") or "gpt-5.5")
    options = {
        key: value
        for key, value in openai.items()
        if key in {"base_url", "request_timeout_seconds", "model", "reasoning_effort", "max_output_tokens", "include_raw_metadata"}
    }
    try:
        client = openai_client_from_config(openai)
        client.version()
        model = client.model_label
    except OpenAIError as exc:
        raise ExtractionEngineError(str(exc)) from exc

    stats.update(ensure_evidence_for_dirty_prs(conn, all_prs=all_prs, pr_ref=pr_ref, config=config))
    prs = limited_prs(select_prs(conn, all_prs=all_prs, pr_ref=pr_ref), model_limit)
    redact_metadata = not bool(openai.get("include_raw_metadata", True))
    for pr in prs:
        chunks = chunks_for_pr(conn, pr["repo_full_name"], int(pr["number"]))
        has_primary = any(int(chunk["is_primary_signal"]) for chunk in chunks)
        if not chunks or (not has_primary and not allow_context_only):
            stats["no_learning_prs"] += 1
            if clear_dirty:
                conn.execute("update prs set dirty=0 where id=?", (pr["id"],))
                conn.commit()
            continue
        if not has_primary:
            stats["context_only_prs_processed"] += 1
        run_result = run_model_extraction(
            conn,
            client,
            source="openai",
            model=model,
            options=options,
            payload=extraction_payload(pr, chunks, redact_metadata=redact_metadata),
            redacted_prompt=redact_metadata,
        )
        if run_result["cache_hit"]:
            stats["model_runs_reused"] += 1
        elif run_result["status"] == "success":
            stats["model_runs_created"] += 1
        else:
            stats["model_runs_failed"] += 1
        if run_result["status"] != "success":
            conn.commit()
            if run_result.get("fatal"):
                raise ExtractionEngineError(str(run_result.get("error") or "OpenAI extraction failed"))
            continue
        stored = store_model_candidates(
            conn,
            run_result["model_run_id"],
            run_result["data"],
            source="openai",
            secret_echo=bool(run_result.get("secret_echo")),
            allow_context_only=allow_context_only,
        )
        for key, value in stored.items():
            stats[key] = int(stats.get(key, 0)) + int(value)
        stats["openai_prs_processed"] += 1
        if clear_dirty:
            conn.execute("update prs set dirty=0 where id=?", (pr["id"],))
        conn.commit()
    conn.commit()
    return stats


def limited_prs(prs: list[sqlite3.Row], model_limit: int | None) -> list[sqlite3.Row]:
    if not model_limit or model_limit < 1:
        return prs
    return prs[:model_limit]


def chunks_for_pr(conn: sqlite3.Connection, repo: str, number: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        select c.*, e.actor_role, e.url, e.path, e.line
        from evidence_chunks c
        join evidence_items e on e.id = c.evidence_id
        where c.repo_full_name=? and c.pr_number=? and c.is_noise=0
        order by c.is_primary_signal desc,
          case c.chunk_kind
            when 'review_comment' then 1
            when 'review_body' then 2
            when 'issue_comment' then 3
            when 'check_annotation' then 4
            when 'failed_check' then 5
            when 'pr_body' then 6
            when 'pr_context' then 7
            when 'patch_hunk' then 8
            when 'check_run' then 9
            else 10
          end,
          c.chunk_index, c.id
        limit 48
        """,
        (repo, number),
    ).fetchall()
    has_pr_body = any(row["chunk_kind"] == "pr_body" for row in rows)
    selected: list[sqlite3.Row] = []
    seen_hashes: set[str] = set()
    for row in rows:
        if has_pr_body and row["chunk_kind"] == "pr_context":
            continue
        marker = str(row["content_hash"])
        if marker in seen_hashes:
            continue
        seen_hashes.add(marker)
        selected.append(row)
        if len(selected) >= 12:
            break
    return selected


def extraction_payload(pr: sqlite3.Row, chunks: list[sqlite3.Row], *, redact_metadata: bool = True) -> dict[str, Any]:
    def identifier(kind: str, value: object | None) -> object | None:
        return redact_identifier(kind, value) if redact_metadata else value

    return {
        "pr": {
            "repo_ref": identifier("repo", pr["repo_full_name"]),
            "pr_ref": identifier("pr", f"{pr['repo_full_name']}#{pr['number']}"),
            "title": redact_text(pr["title"]).text,
            "author_ref": identifier("user", pr["author_login"]),
        },
        "evidence_chunks": [
            {
                "chunk_id": chunk["id"],
                "evidence_id": chunk["evidence_id"],
                "kind": chunk["chunk_kind"],
                "actor_role": chunk["actor_role"],
                "is_primary_signal": bool(chunk["is_primary_signal"]),
                "path_ref": identifier("path", chunk["path"]),
                "line": chunk["line"],
                "url_ref": identifier("url", chunk["url"]),
                "text": chunk["text"],
            }
            for chunk in chunks
        ],
    }


def run_ollama_extraction(conn: sqlite3.Connection, client: Any, *, model: str, options: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return run_model_extraction(conn, client, source="ollama", model=model, options=options, payload=payload, redacted_prompt=True)


def run_model_extraction(
    conn: sqlite3.Connection,
    client: Any,
    *,
    source: str,
    model: str,
    options: dict[str, Any],
    payload: dict[str, Any],
    redacted_prompt: bool,
) -> dict[str, Any]:
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT if redacted_prompt else SYSTEM_PROMPT_UNREDACTED},
        {"role": "user", "content": extraction_user_prompt(payload_json, redacted=redacted_prompt)},
    ]
    input_hash = slug_key([payload_json])
    schema_digest = schema_hash()
    options_json = json.dumps(options, sort_keys=True, separators=(",", ":"))
    run_type = f"{source}_candidate_extraction"
    model_run_id = "mr_" + slug_key([source, "candidate_extraction", model, PROMPT_VERSION, schema_digest, input_hash, options_json])
    cached = conn.execute("select * from model_runs where id=? and status='success'", (model_run_id,)).fetchone()
    if cached:
        try:
            return {"model_run_id": model_run_id, "status": "success", "cache_hit": True, "data": parse_model_response(json.loads(cached["response_json"] or "{}"))}
        except (json.JSONDecodeError, ValueError) as exc:
            return {"model_run_id": model_run_id, "status": "failed", "cache_hit": False, "fatal": False, "error": str(exc)}

    request_payload = {"provider": source, "model": model, "messages": messages, "options": options, "schema": EXTRACTION_SCHEMA}
    request_json = redact_text(json.dumps(request_payload, sort_keys=True)).text
    attempts_before = conn.execute("select attempts from model_runs where id=?", (model_run_id,)).fetchone()
    base_attempts = int(attempts_before["attempts"]) if attempts_before else 0
    last_error = ""
    last_code = "invalid_json"
    last_response_json: str | None = None
    for attempt in range(1, 3):
        try:
            response = client.chat(model=model, messages=messages, options=options, schema=EXTRACTION_SCHEMA)
            content = response_content(response)
            redacted = redact_text(content)
            stored_response = dict(response)
            if isinstance(stored_response.get("message"), dict):
                stored_response["message"] = {**stored_response["message"], "content": redacted.text}
            else:
                stored_response["message"] = {"content": redacted.text}
            last_response_json = json.dumps(stored_response, sort_keys=True)
            data = parse_model_json(redacted.text)
            if not isinstance(data, dict):
                raise ValueError("model output must be a JSON object")
            store_model_run(
                conn,
                model_run_id=model_run_id,
                run_type=run_type,
                model=model,
                prompt_version=PROMPT_VERSION,
                schema_digest=schema_digest,
                input_hash=input_hash,
                options_json=options_json,
                request_json=request_json,
                response_json=last_response_json,
                status="success",
                attempts=base_attempts + attempt,
                error_code=None,
                error=None,
            )
            return {"model_run_id": model_run_id, "status": "success", "cache_hit": False, "data": data, "secret_echo": redacted.changed}
        except json.JSONDecodeError as exc:
            last_error = f"invalid model JSON: {exc}"
            last_code = "invalid_json"
        except ValueError as exc:
            last_error = str(exc)
            last_code = "invalid_json"
        except (OllamaError, CodexError, OpenAIError) as exc:
            last_error = str(exc)
            last_code = getattr(exc, "code", f"{source}_error")
            store_model_run_failure(
                conn,
                model_run_id,
                run_type,
                model,
                schema_digest,
                input_hash,
                options_json,
                request_json,
                base_attempts + attempt,
                last_code,
                last_error,
                response_json=last_response_json,
            )
            return {"model_run_id": model_run_id, "status": "failed", "cache_hit": False, "fatal": True, "error": last_error}
    store_model_run_failure(
        conn,
        model_run_id,
        run_type,
        model,
        schema_digest,
        input_hash,
        options_json,
        request_json,
        base_attempts + 2,
        last_code,
        last_error,
        response_json=last_response_json,
    )
    return {"model_run_id": model_run_id, "status": "failed", "cache_hit": False, "fatal": False, "error": last_error}


def response_content(response: dict[str, Any]) -> str:
    message = response.get("message")
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(response.get("response") or response.get("content") or "")


def parse_model_json(content: str) -> dict[str, Any]:
    text = strip_markdown_fence(content)
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])
    last_error: json.JSONDecodeError | None = None
    data: Any = None
    parsed = False
    for candidate in candidates:
        for attempt in [candidate, balance_json_suffix(candidate)]:
            try:
                data = json.loads(attempt)
                parsed = True
                break
            except json.JSONDecodeError as exc:
                last_error = exc
        if parsed:
            break
    if not parsed:
        assert last_error is not None
        raise last_error
    if isinstance(data, list):
        return {"candidates": data}
    if not isinstance(data, dict):
        raise ValueError("model output must be a JSON object")
    return data


def balance_json_suffix(text: str) -> str:
    stack: list[str] = []
    in_string = False
    escape = False
    for char in text:
        if escape:
            escape = False
            continue
        if char == "\\" and in_string:
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            opener = stack[-1]
            if (opener == "{" and char == "}") or (opener == "[" and char == "]"):
                stack.pop()
    if not stack:
        return text
    suffix = "".join("}" if opener == "{" else "]" for opener in reversed(stack))
    return text + suffix


def strip_markdown_fence(content: str) -> str:
    text = (content or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_model_response(response: dict[str, Any]) -> dict[str, Any]:
    return parse_model_json(response_content(response))


def store_model_run_failure(
    conn: sqlite3.Connection,
    model_run_id: str,
    run_type: str,
    model: str,
    schema_digest: str,
    input_hash: str,
    options_json: str,
    request_json: str,
    attempts: int,
    error_code: str,
    error: str,
    response_json: str | None = None,
) -> None:
    store_model_run(
        conn,
        model_run_id=model_run_id,
        run_type=run_type,
        model=model,
        prompt_version=PROMPT_VERSION,
        schema_digest=schema_digest,
        input_hash=input_hash,
        options_json=options_json,
        request_json=request_json,
        response_json=response_json,
        status="failed",
        attempts=attempts,
        error_code=error_code,
        error=error,
    )


def store_model_run(
    conn: sqlite3.Connection,
    *,
    model_run_id: str,
    run_type: str,
    model: str,
    prompt_version: str,
    schema_digest: str,
    input_hash: str,
    options_json: str,
    request_json: str,
    response_json: str | None,
    status: str,
    attempts: int,
    error_code: str | None,
    error: str | None,
) -> None:
    now = utcnow()
    conn.execute(
        """
        insert into model_runs(id, run_type, model_name, prompt_version, schema_hash, input_hash, options_json,
          request_json, response_json, status, attempts, error_code, error, created_at, updated_at, completed_at)
        values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        on conflict(id) do update set
          response_json=excluded.response_json, status=excluded.status, attempts=excluded.attempts,
          error_code=excluded.error_code, error=excluded.error, updated_at=excluded.updated_at,
          completed_at=excluded.completed_at
        """,
        (
            model_run_id,
            run_type,
            model,
            prompt_version,
            schema_digest,
            input_hash,
            options_json,
            request_json,
            response_json,
            status,
            attempts,
            error_code,
            redact_text(error).text if error else None,
            now,
            now,
            now,
        ),
    )


def store_model_candidates(
    conn: sqlite3.Connection,
    model_run_id: str,
    data: dict[str, Any],
    *,
    source: str = "ollama",
    secret_echo: bool,
    allow_context_only: bool = False,
) -> dict[str, int]:
    stats = {"candidates_created": 0, "candidates_rejected": 0}
    candidates = data.get("candidates")
    if data.get("no_learning_found") and not candidates:
        return stats
    if not isinstance(candidates, list):
        stats["candidates_rejected"] += 1
        return stats
    seen_signatures: set[tuple[str, ...]] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            stats["candidates_rejected"] += 1
            continue
        candidate = limit_candidate_references(candidate)
        signature = candidate_evidence_signature(candidate)
        if signature and signature in seen_signatures:
            stats["candidates_rejected"] += 1
            continue
        if signature:
            seen_signatures.add(signature)
        validation = validate_candidate(conn, candidate, secret_echo=secret_echo, allow_context_only=allow_context_only)
        if validation["errors"]:
            stats["candidates_rejected"] += 1
            continue
        created = upsert_learning_candidate(conn, model_run_id, candidate, validation, source=source)
        stats["candidates_created"] += 1 if created else 0
    return stats


def candidate_evidence_signature(candidate: dict[str, Any]) -> tuple[str, ...]:
    ids = sorted({str(value) for value in candidate.get("evidence_ids") or []})
    return tuple(ids)


def limit_candidate_references(candidate: dict[str, Any], *, limit: int = 4) -> dict[str, Any]:
    normalized = dict(candidate)
    for key in ["evidence_ids", "evidence_chunk_ids", "evidence_quotes"]:
        values = normalized.get(key)
        if not isinstance(values, list):
            continue
        kept: list[Any] = []
        seen: set[str] = set()
        for value in values:
            marker = str(value)
            if marker in seen:
                continue
            seen.add(marker)
            kept.append(value)
            if len(kept) >= limit:
                break
        normalized[key] = kept
    return normalized


def validate_candidate(conn: sqlite3.Connection, candidate: dict[str, Any], *, secret_echo: bool, allow_context_only: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    required = ["title", "lesson", "prevention_rule", "mistake_pattern", "tags", "severity", "confidence", "learning_type", "evidence_ids"]
    for key in required:
        if key not in candidate:
            errors.append(f"missing_{key}")
    evidence_ids = [str(value) for value in candidate.get("evidence_ids") or []]
    chunk_ids = [str(value) for value in candidate.get("evidence_chunk_ids") or []]
    evidence_rows = rows_by_id(conn, "evidence_items", evidence_ids)
    chunk_rows = rows_by_id(conn, "evidence_chunks", chunk_ids)
    if set(evidence_ids) - set(evidence_rows):
        errors.append("nonexistent_evidence_id")
    if chunk_ids and set(chunk_ids) - set(chunk_rows):
        errors.append("nonexistent_chunk_id")
    if not any(int(row["is_primary_signal"]) for row in evidence_rows.values()) and not allow_context_only:
        errors.append("no_primary_evidence")
    if allow_context_only and not evidence_rows:
        errors.append("no_evidence")
    if is_generic_candidate(candidate):
        errors.append("generic_lesson")
    raw_candidate = json.dumps(candidate, sort_keys=True)
    if secret_echo or contains_secret(raw_candidate):
        errors.append("unredacted_secret_echo")
    if str(candidate.get("severity")) not in SEVERITY:
        errors.append("invalid_severity")
    confidence = coerce_confidence(candidate.get("confidence"))
    if confidence is None:
        confidence = 0.0
        errors.append("invalid_confidence")
    if confidence < 0 or confidence > 1:
        errors.append("invalid_confidence")
    return {"errors": errors, "evidence_rows": evidence_rows, "chunk_rows": chunk_rows, "confidence": confidence}


def coerce_confidence(value: Any) -> float | None:
    if isinstance(value, str):
        label = normalize_text(value)
        if label in {"very high", "critical"}:
            return 0.95
        if label == "high":
            return 0.8
        if label == "medium":
            return 0.6
        if label == "low":
            return 0.4
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rows_by_id(conn: sqlite3.Connection, table: str, ids: list[str]) -> dict[str, sqlite3.Row]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    return {row["id"]: row for row in conn.execute(f"select * from {table} where id in ({placeholders})", ids).fetchall()}


def exact_evidence_quotes(evidence_rows: dict[str, sqlite3.Row], chunk_rows: dict[str, sqlite3.Row], quotes: list[Any]) -> list[str]:
    exact_quotes: list[str] = []
    haystack = "\n".join([row["redacted_text"] for row in evidence_rows.values()] + [row["text"] for row in chunk_rows.values()])
    norm_haystack = normalize_text(haystack)
    for quote in quotes:
        redacted_quote = redact_text(str(quote)).text
        norm_quote = normalize_text(redacted_quote)
        if norm_quote and norm_quote not in norm_haystack:
            continue
        if norm_quote:
            exact_quotes.append(redacted_quote)
    return exact_quotes


def is_generic_candidate(candidate: dict[str, Any]) -> bool:
    text = normalize_text(" ".join(str(candidate.get(key) or "") for key in ["title", "lesson", "prevention_rule"]))
    if text in {"add tests", "write tests", "improve code quality", "fix bugs"}:
        return True
    generic = ["add tests", "write more tests", "improve code", "be careful", "fix the issue", "make sure it works"]
    if any(phrase == text or text.startswith(phrase + " ") for phrase in generic):
        return True
    prevention = normalize_text(str(candidate.get("prevention_rule") or ""))
    has_trigger = any(
        word in text
        for word in [
            "when",
            "before",
            "for every",
            "if",
            "after",
            "where",
            "null",
            "undefined",
            "empty",
            "webhook",
            "typecheck",
            "signature",
            "idempotency",
        ]
    )
    has_action = any(word in prevention or word in text for word in ["add", "run", "validate", "verify", "check", "guard", "test", "implement", "avoid"])
    return not (has_trigger and has_action)


def upsert_learning_candidate(conn: sqlite3.Connection, model_run_id: str, candidate: dict[str, Any], validation: dict[str, Any], *, source: str = "ollama") -> bool:
    tags = tuple(str(tag) for tag in candidate.get("tags") or [])
    key = canonical_key(str(candidate["title"]), str(candidate["prevention_rule"]), tags)
    if conn.execute("select id from learning_cards where canonical_key=? and status='rejected'", (key,)).fetchone():
        return False
    if conn.execute("select id from learning_candidates where canonical_key=? and source=? and status='rejected'", (key, source)).fetchone():
        return False
    existing_card = conn.execute("select id from learning_cards where canonical_key=? and status!='rejected'", (key,)).fetchone()
    candidate_id = "cand_" + slug_key([source, key])
    existed = conn.execute("select status from learning_candidates where id=?", (candidate_id,)).fetchone()
    now = utcnow()
    contexts = sorted({row["repo_full_name"] for row in validation["evidence_rows"].values()})
    raw_candidate = redact_text(json.dumps(candidate, sort_keys=True)).text
    conn.execute(
        """
        insert into learning_candidates(id, source, canonical_key, title, lesson, mistake_pattern, prevention_rule,
          tags_json, contexts_json, severity, confidence, learning_type, status, model_run_id, duplicate_of_card_id,
          validation_errors_json, raw_candidate_json, created_at, updated_at)
        values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        on conflict(canonical_key, source) do update set
          title=excluded.title, lesson=excluded.lesson, mistake_pattern=excluded.mistake_pattern,
          prevention_rule=excluded.prevention_rule, tags_json=excluded.tags_json, contexts_json=excluded.contexts_json,
          severity=excluded.severity, confidence=excluded.confidence, learning_type=excluded.learning_type,
          model_run_id=excluded.model_run_id, duplicate_of_card_id=coalesce(learning_candidates.duplicate_of_card_id, excluded.duplicate_of_card_id),
          validation_errors_json=excluded.validation_errors_json, raw_candidate_json=excluded.raw_candidate_json,
          updated_at=excluded.updated_at,
          status=case when learning_candidates.status in ('accepted','rejected','merged') then learning_candidates.status else excluded.status end
        """,
        (
            candidate_id,
            source,
            key,
            str(candidate["title"]),
            str(candidate["lesson"]),
            str(candidate.get("mistake_pattern") or ""),
            str(candidate["prevention_rule"]),
            json.dumps(list(tags), sort_keys=True),
            json.dumps(contexts, sort_keys=True),
            SEVERITY[str(candidate["severity"])],
            validation["confidence"],
            str(candidate["learning_type"]),
            "pending",
            model_run_id,
            int(existing_card["id"]) if existing_card else None,
            "[]",
            raw_candidate,
            now,
            now,
        ),
    )
    attach_candidate_evidence(conn, candidate_id, candidate, validation)
    current = conn.execute("select status, duplicate_of_card_id from learning_candidates where id=?", (candidate_id,)).fetchone()
    if current and current["status"] == "accepted" and current["duplicate_of_card_id"]:
        add_candidate_to_card(conn, candidate_id, int(current["duplicate_of_card_id"]), status="accepted")
    return existed is None


def attach_candidate_evidence(conn: sqlite3.Connection, candidate_id: str, candidate: dict[str, Any], validation: dict[str, Any]) -> None:
    evidence_ids = [str(value) for value in candidate.get("evidence_ids") or []]
    chunk_ids = [str(value) for value in candidate.get("evidence_chunk_ids") or []]
    chunk_rows = dict(validation["chunk_rows"])
    quotes = exact_evidence_quotes(validation["evidence_rows"], chunk_rows, candidate.get("evidence_quotes") or [])
    if not chunk_ids and evidence_ids:
        placeholders = ",".join("?" for _ in evidence_ids)
        rows = conn.execute(f"select * from evidence_chunks where evidence_id in ({placeholders}) order by is_primary_signal desc, chunk_index", evidence_ids).fetchall()
        chunk_rows = {row["id"]: row for row in rows}
        chunk_ids = [row["id"] for row in rows]
        quotes = exact_evidence_quotes(validation["evidence_rows"], chunk_rows, candidate.get("evidence_quotes") or [])
    for index, evidence_id in enumerate(evidence_ids):
        matching_chunks = [chunk_id for chunk_id in chunk_ids if chunk_id in chunk_rows and chunk_rows[chunk_id]["evidence_id"] == evidence_id]
        for chunk_id in matching_chunks:
            conn.execute(
                """
                insert or ignore into learning_candidate_evidence(candidate_id, evidence_id, chunk_id, quote, role, created_at)
                values(?,?,?,?,?,?)
                """,
                (
                    candidate_id,
                    evidence_id,
                    chunk_id,
                    quotes[min(index, len(quotes) - 1)] if quotes else None,
                    "primary" if int(validation["evidence_rows"][evidence_id]["is_primary_signal"]) else "supporting",
                    utcnow(),
                ),
            )


def dedupe(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("select * from learning_cards where status != 'rejected' order by id").fetchall()
    seen: dict[str, sqlite3.Row] = {}
    merged = 0
    for row in rows:
        key = canonical_key(row["title"], row["prevention_rule"], json.loads(row["tags_json"]))
        if key not in seen:
            if key != row["canonical_key"]:
                conn.execute("update learning_cards set canonical_key=?, updated_at=? where id=?", (key, utcnow(), row["id"]))
            seen[key] = row
            continue
        target = seen[key]
        conn.execute("update learning_evidence set learning_id=? where learning_id=?", (target["id"], row["id"]))
        recurrence = evidence_count(conn, int(target["id"]))
        conn.execute(
            "update learning_cards set recurrence_count=?, updated_at=?, last_seen_at=max(last_seen_at, ?) where id=?",
            (recurrence, utcnow(), row["last_seen_at"], target["id"]),
        )
        conn.execute("delete from learning_cards where id=?", (row["id"],))
        merged += 1
    conn.commit()
    return {"merged": merged, "cards": len(seen)}


def parse_pr_ref(value: str) -> tuple[str, int]:
    if "#" not in value:
        raise ValueError("--pr must look like owner/repo#123")
    repo, number = value.rsplit("#", 1)
    return repo, int(number)


def update_status(conn: sqlite3.Connection, card_id: int, status: str) -> bool:
    cur = conn.execute("update learning_cards set status=?, updated_at=? where id=?", (status, utcnow(), card_id))
    conn.commit()
    return cur.rowcount > 0


def merge_cards(conn: sqlite3.Connection, source_id: int, target_id: int) -> bool:
    if source_id == target_id:
        return False
    source = conn.execute("select * from learning_cards where id=?", (source_id,)).fetchone()
    target = conn.execute("select * from learning_cards where id=?", (target_id,)).fetchone()
    if not source or not target:
        return False
    conn.execute("update learning_evidence set learning_id=? where learning_id=?", (target_id, source_id))
    recurrence = evidence_count(conn, target_id)
    contexts = sorted(set(json.loads(source["contexts_json"]) + json.loads(target["contexts_json"])))
    conn.execute(
        "update learning_cards set recurrence_count=?, contexts_json=?, updated_at=?, last_seen_at=max(last_seen_at, ?) where id=?",
        (recurrence, json.dumps(contexts), utcnow(), source["last_seen_at"], target_id),
    )
    conn.execute("delete from learning_cards where id=?", (source_id,))
    conn.commit()
    return True


def candidate_rows(conn: sqlite3.Connection, *, status: str | None = None, limit: int | None = None) -> list[sqlite3.Row]:
    params: list[object] = []
    where = ""
    if status:
        where = "where status=?"
        params.append(status)
    rows = conn.execute(f"select * from learning_candidates {where} order by status, confidence desc, updated_at desc", params).fetchall()
    return rows[:limit] if limit else rows


def candidate_to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    evidence = conn.execute(
        """
        select ce.*, e.repo_full_name, e.pr_number, e.source_kind, e.url, e.path, e.line
        from learning_candidate_evidence ce
        join evidence_items e on e.id = ce.evidence_id
        where ce.candidate_id=?
        order by ce.role, ce.created_at
        """,
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "source": row["source"],
        "canonical_key": row["canonical_key"],
        "title": row["title"],
        "lesson": row["lesson"],
        "mistake_pattern": row["mistake_pattern"],
        "prevention_rule": row["prevention_rule"],
        "tags": json.loads(row["tags_json"]),
        "contexts": json.loads(row["contexts_json"]),
        "severity": row["severity"],
        "confidence": row["confidence"],
        "learning_type": row["learning_type"],
        "status": row["status"],
        "duplicate_of_card_id": row["duplicate_of_card_id"],
        "evidence": [
            {
                "evidence_id": item["evidence_id"],
                "chunk_id": item["chunk_id"],
                "role": item["role"],
                "repo_full_name": item["repo_full_name"],
                "pr_number": item["pr_number"],
                "source_kind": item["source_kind"],
                "quote": item["quote"],
                "url": item["url"],
                "path": item["path"],
                "line": item["line"],
            }
            for item in evidence
        ],
    }


def accept_candidate(conn: sqlite3.Connection, candidate_id: str) -> int | None:
    row = conn.execute("select * from learning_candidates where id=?", (candidate_id,)).fetchone()
    if not row or row["status"] == "rejected":
        return None
    existing = conn.execute("select * from learning_cards where canonical_key=?", (row["canonical_key"],)).fetchone()
    if existing and existing["status"] == "rejected":
        reject_candidate(conn, candidate_id, "matching card was rejected")
        return None
    if existing:
        card_id = int(existing["id"])
    else:
        now = utcnow()
        card_id = int(
            conn.execute(
                """
                insert into learning_cards(canonical_key, title, lesson, mistake_pattern, prevention_rule, tags_json,
                  contexts_json, severity, confidence, status, recurrence_count, first_seen_at, last_seen_at, created_at, updated_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["canonical_key"],
                    row["title"],
                    row["lesson"],
                    row["mistake_pattern"],
                    row["prevention_rule"],
                    row["tags_json"],
                    row["contexts_json"],
                    row["severity"],
                    row["confidence"],
                    "accepted",
                    1,
                    now,
                    now,
                    now,
                    now,
                ),
            ).lastrowid
        )
    add_candidate_to_card(conn, candidate_id, card_id, status="accepted")
    conn.commit()
    return card_id


def merge_candidate(conn: sqlite3.Connection, candidate_id: str, target_id: int) -> bool:
    target = conn.execute("select id from learning_cards where id=? and status!='rejected'", (target_id,)).fetchone()
    candidate = conn.execute("select id from learning_candidates where id=? and status!='rejected'", (candidate_id,)).fetchone()
    if not target or not candidate:
        return False
    add_candidate_to_card(conn, candidate_id, target_id, status="merged")
    conn.commit()
    return True


def reject_candidate(conn: sqlite3.Connection, candidate_id: str, reason: str | None = None) -> bool:
    cur = conn.execute("update learning_candidates set status='rejected', updated_at=? where id=?", (utcnow(), candidate_id))
    if cur.rowcount:
        conn.execute(
            "insert into learning_card_feedback(candidate_id, action, reason, created_at) values(?,?,?,?)",
            (candidate_id, "reject", reason, utcnow()),
        )
    conn.commit()
    return cur.rowcount > 0


def add_candidate_to_card(conn: sqlite3.Connection, candidate_id: str, card_id: int, *, status: str) -> int:
    candidate = conn.execute("select * from learning_candidates where id=?", (candidate_id,)).fetchone()
    card = conn.execute("select * from learning_cards where id=?", (card_id,)).fetchone()
    if not candidate or not card:
        return 0
    evidence_rows = conn.execute(
        """
        select ce.*, e.source_table, e.source_id, e.repo_full_name, e.pr_number, e.redacted_text, e.url
        from learning_candidate_evidence ce
        join evidence_items e on e.id = ce.evidence_id
        where ce.candidate_id=?
        order by e.repo_full_name, e.pr_number, ce.evidence_id
        """,
        (candidate_id,),
    ).fetchall()
    evidence_hash = slug_key([candidate_id] + [row["evidence_id"] for row in evidence_rows])
    recurrence_added = 0
    seen_prs: set[tuple[str, int]] = set()
    for evidence in evidence_rows:
        event_id = int(evidence["source_id"]) if evidence["source_table"] == "events" and str(evidence["source_id"]).isdigit() else None
        quote = evidence["quote"] or (evidence["redacted_text"] or "")[:500]
        conn.execute(
            """
            insert or ignore into learning_evidence(learning_id, event_id, repo_full_name, pr_number, quote, url, created_at)
            values(?,?,?,?,?,?,?)
            """,
            (card_id, event_id, evidence["repo_full_name"], evidence["pr_number"], quote, evidence["url"], utcnow()),
        )
        pr_key = (evidence["repo_full_name"], int(evidence["pr_number"]))
        if pr_key in seen_prs:
            continue
        seen_prs.add(pr_key)
        recurrence_id = "rec_" + slug_key([str(card_id), candidate["canonical_key"], pr_key[0], str(pr_key[1]), evidence_hash])
        before = conn.execute("select id from learning_card_recurrences where id=?", (recurrence_id,)).fetchone()
        conn.execute(
            """
            insert or ignore into learning_card_recurrences(id, learning_id, candidate_id, canonical_key, repo_full_name, pr_number, evidence_hash, created_at)
            values(?,?,?,?,?,?,?,?)
            """,
            (recurrence_id, card_id, candidate_id, candidate["canonical_key"], pr_key[0], pr_key[1], evidence_hash, utcnow()),
        )
        if before is None:
            recurrence_added += 1
    contexts = sorted(set(json.loads(card["contexts_json"]) + json.loads(candidate["contexts_json"])))
    recurrence_rows = conn.execute("select count(*) as count from learning_card_recurrences where learning_id=?", (card_id,)).fetchone()["count"]
    recurrence = max(int(card["recurrence_count"]), int(recurrence_rows), evidence_count(conn, card_id))
    conn.execute(
        """
        update learning_cards set recurrence_count=?, contexts_json=?, confidence=max(confidence, ?),
          severity=max(severity, ?), last_seen_at=?, updated_at=? where id=?
        """,
        (recurrence, json.dumps(contexts), candidate["confidence"], candidate["severity"], utcnow(), utcnow(), card_id),
    )
    conn.execute("update learning_candidates set status=?, duplicate_of_card_id=?, updated_at=? where id=?", (status, card_id, utcnow(), candidate_id))
    conn.execute(
        "insert into learning_card_feedback(learning_id, candidate_id, action, created_at) values(?,?,?,?)",
        (card_id, candidate_id, status, utcnow()),
    )
    return recurrence_added
