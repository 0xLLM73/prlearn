from __future__ import annotations

import copy
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from .config import github_config, load_config, save_config
from .db import connect, init_home, migrate
from .export import export_all, learning_rows, row_to_dict
from .github import FixtureGitHubClient, GhCliClient, GitHubClient, github_app_client_from_config, github_app_configured
from .learn import ExtractionEngineError, candidate_rows, candidate_to_dict, dedupe, extract
from .seed import seed_account
from .sync import sync_prs


DEFAULT_EXPECTED_TITLES = [
    "Cover null and empty states with regression tests",
    "Run typecheck and build checks before opening PRs",
    "Verify webhook signatures before trusting payloads",
]


def evaluate_fixtures(
    *,
    fixture: Path,
    incremental_fixture: Path,
    home: Path | None = None,
    engine: str = "heuristic",
    expected_cards: int = 3,
    expected_null_recurrence: int = 3,
    min_events: int = 7,
    expected_titles: list[str] | None = None,
    keep_home: bool = False,
) -> dict[str, Any]:
    work_home = home or Path(tempfile.mkdtemp(prefix="prlearn-eval-"))
    cleanup = home is None and not keep_home
    try:
        work_home.mkdir(parents=True, exist_ok=True)
        db_path = work_home / "prlearn.db"
        init_home(work_home, db_path)
        conn = connect(db_path)
        migrate(conn)
        try:
            config = load_config(work_home, db_path)
            first = run_fixture_pass(conn, work_home, fixture, engine, config)
            second = run_fixture_pass(conn, work_home, fixture, engine, config)
            incremental = run_fixture_pass(conn, work_home, incremental_fixture, engine, config)
            cards = [row_to_dict(conn, row) for row in learning_rows(conn)]
            metrics = collect_metrics(conn, cards, first, second, incremental, work_home)
        finally:
            conn.close()
        checks = build_checks(
            metrics,
            expected_cards=expected_cards,
            expected_null_recurrence=expected_null_recurrence,
            min_events=min_events,
            expected_titles=expected_titles or DEFAULT_EXPECTED_TITLES,
        )
        return {
            "passed": all(check["ok"] for check in checks),
            "engine": engine,
            "fixture": str(fixture),
            "incremental_fixture": str(incremental_fixture),
            "home": str(work_home) if keep_home or home else None,
            "metrics": metrics,
            "checks": checks,
        }
    finally:
        if cleanup:
            shutil.rmtree(work_home, ignore_errors=True)


def run_fixture_pass(conn: Any, home: Path, fixture: Path, engine: str, config: dict[str, Any]) -> dict[str, Any]:
    sync_stats = sync_prs(conn, FixtureGitHubClient(fixture), {"incremental": False, "dry_run": False, "config": config})
    extract_stats = extract(conn, engine=engine, config=config)
    dedupe_stats = dedupe(conn)
    exports = export_all(conn, home / "exports", "all")
    return {"sync": sync_stats, "extract": extract_stats, "dedupe": dedupe_stats, "exports": exports}


def compare_engines(
    *,
    fixture: Path,
    incremental_fixture: Path | None = None,
    engines: list[str] | None = None,
    home: Path | None = None,
    keep_home: bool = False,
    allow_context_only: bool = False,
) -> dict[str, Any]:
    selected = engines or ["heuristic", "ollama", "codex"]
    root_home = home or Path(tempfile.mkdtemp(prefix="prlearn-compare-"))
    cleanup = home is None and not keep_home
    results: list[dict[str, Any]] = []
    try:
        root_home.mkdir(parents=True, exist_ok=True)
        for engine in selected:
            engine_home = root_home / engine
            engine_home.mkdir(parents=True, exist_ok=True)
            db_path = engine_home / "prlearn.db"
            init_home(engine_home, db_path)
            conn = connect(db_path)
            migrate(conn)
            try:
                config = load_config(engine_home, db_path)
                sync_stats = [sync_prs(conn, FixtureGitHubClient(fixture), {"incremental": False, "dry_run": False, "config": config})]
                if incremental_fixture:
                    sync_stats.append(sync_prs(conn, FixtureGitHubClient(incremental_fixture), {"incremental": False, "dry_run": False, "config": config}))
                try:
                    extract_stats = extract(conn, engine=engine, config=config, allow_context_only=allow_context_only)
                    dedupe_stats = dedupe(conn)
                    results.append(
                        {
                            "engine": engine,
                            "ok": True,
                            "home": str(engine_home) if keep_home or home else None,
                            "prs_seen": sum(int(item.get("prs_seen", 0)) for item in sync_stats),
                            "sync": sync_stats,
                            "extract": extract_stats,
                            "dedupe": dedupe_stats,
                            "cards": compact_cards(conn),
                            "candidates": compact_candidates(conn),
                        }
                    )
                except ExtractionEngineError as exc:
                    results.append(
                        {
                            "engine": engine,
                            "ok": False,
                            "home": str(engine_home) if keep_home or home else None,
                            "prs_seen": sum(int(item.get("prs_seen", 0)) for item in sync_stats),
                            "error": str(exc),
                            "cards": compact_cards(conn),
                            "candidates": compact_candidates(conn),
                        }
                    )
            finally:
                conn.close()
        return {
            "passed": all(result["ok"] for result in results),
            "fixture": str(fixture),
            "incremental_fixture": str(incremental_fixture) if incremental_fixture else None,
            "home": str(root_home) if keep_home or home else None,
            "engines": results,
        }
    finally:
        if cleanup:
            shutil.rmtree(root_home, ignore_errors=True)


def compare_live_engines(
    *,
    base_home: Path,
    base_db_path: Path,
    engines: list[str],
    seed_options: dict[str, Any],
    github_auth: str | None,
    keep_home: bool = False,
) -> dict[str, Any]:
    root_home = Path(tempfile.mkdtemp(prefix="prlearn-live-compare-"))
    results: list[dict[str, Any]] = []
    init_home(base_home, base_db_path)
    base_config = load_config(base_home, base_db_path)
    try:
        root_home.mkdir(parents=True, exist_ok=True)
        for engine in engines:
            engine_home = root_home / engine
            engine_db = engine_home / "prlearn.db"
            engine_home.mkdir(parents=True, exist_ok=True)
            init_home(engine_home, engine_db)
            config = copy.deepcopy(base_config)
            config["db"] = str(engine_db)
            save_config(engine_home, config)
            conn = connect(engine_db)
            migrate(conn)
            started = time.monotonic()
            try:
                options = {**seed_options, "engine": engine, "config": config, "dry_run": False}
                try:
                    stats = seed_account(conn, github_client_for(config, github_auth), options)
                    dedupe_stats = dedupe(conn)
                    elapsed = round(time.monotonic() - started, 3)
                    results.append(
                        {
                            "engine": engine,
                            "ok": True,
                            "home": str(engine_home) if keep_home else None,
                            "elapsed_seconds": elapsed,
                            "repos_seen": stats.get("repos_seen", 0),
                            "repos_selected": stats.get("repos_selected", 0),
                            "selected_repos": stats.get("selected_repos", []),
                            "prs_seen": stats.get("prs_seen", 0),
                            "commits_seen": stats.get("commits_seen", 0),
                            "seed": stats,
                            "dedupe": dedupe_stats,
                            "cards": compact_cards(conn),
                            "candidates": compact_candidates(conn),
                        }
                    )
                except Exception as exc:
                    elapsed = round(time.monotonic() - started, 3)
                    results.append(
                        {
                            "engine": engine,
                            "ok": False,
                            "home": str(engine_home) if keep_home else None,
                            "elapsed_seconds": elapsed,
                            "error": str(exc),
                            "cards": compact_cards(conn),
                            "candidates": compact_candidates(conn),
                        }
                    )
            finally:
                conn.close()
        return {
            "passed": all(result["ok"] for result in results),
            "home": str(root_home) if keep_home else None,
            "engines": results,
        }
    finally:
        if not keep_home:
            shutil.rmtree(root_home, ignore_errors=True)


def github_client_for(config: dict[str, Any], mode: str | None) -> GitHubClient:
    github = github_config(config)
    selected = mode or str(github.get("auth_mode") or "auto")
    if selected == "app":
        return github_app_client_from_config(github)
    if selected == "auto" and github_app_configured(github):
        return github_app_client_from_config(github)
    return GhCliClient()


def compact_cards(conn: Any) -> list[dict[str, Any]]:
    return [
        {
            "title": card["title"],
            "lesson": card["lesson"],
            "prevention_rule": card["prevention_rule"],
            "tags": card["tags"],
            "severity": card["severity"],
            "confidence": card["confidence"],
            "recurrence_count": card["recurrence_count"],
            "evidence_count": len(card.get("evidence") or []),
        }
        for card in [row_to_dict(conn, row) for row in learning_rows(conn)]
    ]


def compact_candidates(conn: Any) -> list[dict[str, Any]]:
    return [
        {
            "source": candidate["source"],
            "title": candidate["title"],
            "lesson": candidate["lesson"],
            "prevention_rule": candidate["prevention_rule"],
            "tags": candidate["tags"],
            "severity": candidate["severity"],
            "confidence": candidate["confidence"],
            "status": candidate["status"],
            "learning_type": candidate["learning_type"],
            "evidence_count": len(candidate.get("evidence") or []),
        }
        for candidate in [candidate_to_dict(conn, row) for row in candidate_rows(conn)]
    ]


def collect_metrics(conn: Any, cards: list[dict[str, Any]], first: dict[str, Any], second: dict[str, Any], incremental: dict[str, Any], home: Path) -> dict[str, Any]:
    card_count = int(conn.execute("select count(*) as count from learning_cards").fetchone()["count"])
    event_count = int(conn.execute("select count(*) as count from events").fetchone()["count"])
    candidate_count = int(conn.execute("select count(*) as count from learning_candidates").fetchone()["count"])
    null_row = conn.execute("select * from learning_cards where mistake_pattern='tests-null-state'").fetchone()
    null_recurrence = int(null_row["recurrence_count"]) if null_row else 0
    null_evidence = 0
    if null_row:
        null_evidence = int(conn.execute("select count(*) as count from learning_evidence where learning_id=?", (null_row["id"],)).fetchone()["count"])
    export_paths = [home / "exports" / name for name in ["LEARNINGS.md", "context.json", "rules.md"]]
    return {
        "card_count": card_count,
        "event_count": event_count,
        "candidate_count": candidate_count,
        "titles": sorted(card["title"] for card in cards),
        "null_state_recurrence": null_recurrence,
        "null_state_evidence": null_evidence,
        "first_cards_created": first["extract"].get("created", 0),
        "second_cards_created": second["extract"].get("created", 0),
        "incremental_cards_created": incremental["extract"].get("created", 0),
        "first_events_upserted": first["sync"].get("events_upserted", 0),
        "second_events_upserted": second["sync"].get("events_upserted", 0),
        "incremental_events_upserted": incremental["sync"].get("events_upserted", 0),
        "exports_exist": all(path.exists() for path in export_paths),
    }


def build_checks(
    metrics: dict[str, Any],
    *,
    expected_cards: int,
    expected_null_recurrence: int,
    min_events: int,
    expected_titles: list[str],
) -> list[dict[str, Any]]:
    titles = set(metrics["titles"])
    expected_title_set = set(expected_titles)
    return [
        check("card_count", metrics["card_count"] == expected_cards, expected_cards, metrics["card_count"]),
        check("min_event_count", metrics["event_count"] >= min_events, f">={min_events}", metrics["event_count"]),
        check("idempotent_second_run", metrics["second_cards_created"] == 0 and metrics["second_events_upserted"] == 0, "0 new cards/events", {"cards": metrics["second_cards_created"], "events": metrics["second_events_upserted"]}),
        check("incremental_recurrence", metrics["null_state_recurrence"] >= expected_null_recurrence, f">={expected_null_recurrence}", metrics["null_state_recurrence"]),
        check("null_state_evidence", metrics["null_state_evidence"] >= expected_null_recurrence, f">={expected_null_recurrence}", metrics["null_state_evidence"]),
        check("expected_titles", expected_title_set.issubset(titles), sorted(expected_title_set), metrics["titles"]),
        check("exports_exist", bool(metrics["exports_exist"]), True, metrics["exports_exist"]),
    ]


def check(name: str, ok: bool, expected: Any, actual: Any) -> dict[str, Any]:
    return {"name": name, "ok": ok, "expected": expected, "actual": actual}
