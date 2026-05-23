from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from .config import github_config, load_config, save_config
from .crypto import EncryptionError
from .db import connect, init_home, migrate
from .doctor import check as doctor_check, overall_ok
from .evaluate import DEFAULT_EXPECTED_TITLES, compare_engines, compare_live_engines, evaluate_fixtures
from .export import export_all, learning_rows, row_to_dict
from .github import FixtureGitHubClient, GhCliClient, GitHubClientError, github_app_client_from_config, github_app_configured
from .learn import ExtractionEngineError
from .learn import accept_candidate as accept_learning_candidate
from .learn import candidate_rows, candidate_to_dict
from .learn import dedupe as dedupe_cards
from .learn import extract as extract_cards
from .learn import merge_candidate as merge_learning_candidate
from .learn import merge_cards, update_status
from .learn import reject_candidate as reject_learning_candidate
from .preflight import preflight_report, render_preflight
from .privacy import decrypt_raw_json, encrypt_raw_json, raw_json_status
from .report import daily_summary, render_daily_summary, write_daily_report
from .schedule import install_schedule, schedule_text, status as schedule_status, uninstall_schedule
from .seed import seed_account
from .sync import sync_prs
from .telegram_bot import TelegramClient, TelegramError, apply_candidate_rating, discover_chats, poll_ratings, send_pending_candidates, settings_from_env, token_from_env
from .usefulness import actionable_insights_report, card_summary, candidate_summary, readiness_score, render_actionable_insights_report, render_usefulness_report, usefulness_report
from .util import ensure_home, is_pid_alive, resolve_db, resolve_home, utcnow

MODEL_ENGINES = ["heuristic", "ollama", "hybrid", "codex", "openai"]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prlearn")
    sub = parser.add_subparsers(dest="command")
    add_common(sub.add_parser("init", help="Initialize local prlearn storage")).set_defaults(func=cmd_init)
    p = add_common(sub.add_parser("doctor", help="Check local prerequisites"))
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--strict-ollama", action="store_true")
    p.set_defaults(func=cmd_doctor)
    p = add_sync_args(add_common(sub.add_parser("sync", help="Sync PR data")))
    p.set_defaults(func=cmd_sync)
    p = add_seed_args(add_common(sub.add_parser("seed", help="Seed learnings from a GitHub account")))
    p.set_defaults(func=cmd_seed)
    p = add_common(sub.add_parser("extract", help="Extract learning candidates"))
    p.add_argument("--all", action="store_true")
    p.add_argument("--pr")
    p.add_argument("--fixture")
    p.add_argument("--min-confidence", type=float, default=0.55)
    p.add_argument("--engine", choices=MODEL_ENGINES, default="heuristic")
    p.add_argument("--provider", choices=MODEL_ENGINES)
    p.add_argument("--allow-context-only", action="store_true", help="Allow Ollama modes to propose reviewable candidates from PR context when no primary feedback signal exists")
    p.add_argument("--max-model-prs", type=int, help="Limit model-provider extraction calls without limiting synced PRs")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_extract)
    p = add_common(sub.add_parser("dedupe", help="Dedupe learning cards"))
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_dedupe)
    p = add_sync_args(add_common(sub.add_parser("daily", help="Run daily sync/extract/export/report")))
    p.add_argument("--engine", choices=MODEL_ENGINES, default="heuristic")
    p.add_argument("--provider", choices=MODEL_ENGINES)
    p.add_argument("--allow-context-only", action="store_true", help="Allow Ollama modes to propose reviewable candidates from PR context when no primary feedback signal exists")
    p.add_argument("--max-model-prs", type=int, help="Limit model-provider extraction calls without limiting synced PRs")
    p.set_defaults(func=cmd_daily)
    p = add_common(sub.add_parser("eval", help="Run deterministic fixture evaluation checks"))
    p.add_argument("--fixture", default="tests/fixtures/github_small.json")
    p.add_argument("--incremental-fixture", default="tests/fixtures/github_incremental.json")
    p.add_argument("--engine", choices=MODEL_ENGINES, default="heuristic")
    p.add_argument("--expected-cards", type=int, default=3)
    p.add_argument("--expected-null-recurrence", type=int, default=3)
    p.add_argument("--min-events", type=int, default=7)
    p.add_argument("--expect-title", action="append", dest="expected_titles")
    p.add_argument("--keep-home", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_eval)
    p = add_common(sub.add_parser("compare", help="Compare extraction output across engines on fixture PRs"))
    p.add_argument("--fixture", default="tests/fixtures/github_small.json")
    p.add_argument("--incremental-fixture", default="tests/fixtures/github_incremental.json")
    p.add_argument("--engines", default="heuristic,ollama,codex", help="Comma-separated engines to compare")
    p.add_argument("--allow-context-only", action="store_true")
    p.add_argument("--keep-home", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_compare)
    p = add_seed_args(add_common(sub.add_parser("compare-live", help="Compare live GitHub extraction output across engines")))
    p.add_argument("--engines", default="heuristic,ollama,codex", help="Comma-separated engines to compare")
    p.add_argument("--keep-home", action="store_true")
    p.set_defaults(func=cmd_compare_live)
    p = add_common(sub.add_parser("list", help="List learning cards"))
    p.add_argument("--status", choices=["accepted", "pending", "rejected", "archived"])
    for status in ["accepted", "pending", "rejected", "archived"]:
        p.add_argument(f"--{status}", action="store_true")
    p.add_argument("--tag")
    p.add_argument("--repo")
    p.add_argument("--json", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_list)
    p = add_common(sub.add_parser("review", help="Interactively review pending cards"))
    p.add_argument("--candidates", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--min-score", type=float)
    p.add_argument("--ready", action="store_true")
    p.set_defaults(func=cmd_review)
    for name, func in [("accept", cmd_accept), ("reject", cmd_reject), ("archive", cmd_archive)]:
        p = add_common(sub.add_parser(name, help=f"{name.title()} a learning card"))
        p.add_argument("id", type=int)
        p.set_defaults(func=func)
    p = add_common(sub.add_parser("merge", help="Merge source ID into target ID"))
    p.add_argument("source_id", type=int)
    p.add_argument("target_id", type=int)
    p.set_defaults(func=cmd_merge)
    p = add_common(sub.add_parser("accept-candidate", help="Accept an Ollama learning candidate"))
    p.add_argument("id")
    p.set_defaults(func=cmd_accept_candidate)
    p = add_common(sub.add_parser("reject-candidate", help="Reject an Ollama learning candidate"))
    p.add_argument("id")
    p.add_argument("--reason")
    p.set_defaults(func=cmd_reject_candidate)
    p = add_common(sub.add_parser("merge-candidate", help="Merge an Ollama learning candidate into an existing card"))
    p.add_argument("id")
    p.add_argument("card_id", type=int)
    p.set_defaults(func=cmd_merge_candidate)
    telegram = sub.add_parser("telegram", help="Send and rate learning candidates through Telegram")
    telegram_sub = telegram.add_subparsers(dest="telegram_command", required=True)
    p = add_common(telegram_sub.add_parser("status", help="Show Telegram bot readiness"))
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_telegram_status)
    p = add_common(telegram_sub.add_parser("chats", help="List chat IDs from recent bot updates"))
    p.add_argument("--timeout", type=int, default=0)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_telegram_chats)
    p = add_common(telegram_sub.add_parser("send", help="Send pending candidates to Telegram with rating buttons"))
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--ready", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_telegram_send)
    p = add_common(telegram_sub.add_parser("poll", help="Poll Telegram once and apply rating button clicks"))
    p.add_argument("--timeout", type=int, default=0)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--keep-reviewed", action="store_true", help="Keep Telegram review messages after ratings are recorded")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_telegram_poll)
    p = add_common(telegram_sub.add_parser("rate", help="Apply a candidate rating without Telegram network calls"))
    p.add_argument("candidate_id")
    p.add_argument("rating", choices=["major", "minor", "not_important"])
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_telegram_rate)
    privacy = sub.add_parser("privacy", help="Inspect or encrypt sensitive local raw GitHub payloads")
    privacy_sub = privacy.add_subparsers(dest="privacy_command", required=True)
    p = add_common(privacy_sub.add_parser("status", help="Show raw payload encryption status"))
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_privacy_status)
    p = add_common(privacy_sub.add_parser("encrypt-raw", help="Encrypt existing raw GitHub payload columns"))
    p.add_argument("--prompt-passphrase", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_privacy_encrypt_raw)
    p = add_common(privacy_sub.add_parser("decrypt-raw", help="Decrypt existing raw GitHub payload columns"))
    p.add_argument("--prompt-passphrase", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_privacy_decrypt_raw)
    p = add_common(sub.add_parser("preflight", help="Show relevant learnings before coding"))
    p.add_argument("paths", nargs="*")
    p.add_argument("--paths", nargs="+", action="append", dest="paths_option")
    p.add_argument("--repo", default=".")
    p.add_argument("--task", default="")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--include-pending", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_preflight)
    p = add_common(sub.add_parser("insights", help="Report whether prlearn is learning useful things"))
    p.add_argument("--actionable", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_insights)
    p = add_common(sub.add_parser("export", help="Export learning files"))
    p.add_argument("--out")
    p.add_argument("--format", choices=["markdown", "json", "all"], default="all")
    p.add_argument("--include-pending", action="store_true")
    p.set_defaults(func=cmd_export)
    schedule = sub.add_parser("schedule", help="Manage daily scheduler")
    schedule_sub = schedule.add_subparsers(dest="schedule_command", required=True)
    for name, func in [("install", cmd_schedule_install), ("status", cmd_schedule_status), ("uninstall", cmd_schedule_uninstall), ("print", cmd_schedule_print)]:
        p = add_common(schedule_sub.add_parser(name))
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=func)
    return parser


def add_common(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--home")
    parser.add_argument("--db")
    return parser


def add_sync_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--author", default="@me")
    parser.add_argument("--github-auth", choices=["auto", "app", "gh"])
    parser.add_argument("--since")
    parser.add_argument("--incremental", action="store_true")
    parser.add_argument("--lookback-days", type=int, default=3)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--repo", action="append", dest="repos")
    parser.add_argument("--owner", action="append", dest="owners")
    parser.add_argument("--fixture")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def add_seed_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--author", default="@me")
    parser.add_argument("--github-auth", choices=["auto", "app", "gh"])
    parser.add_argument("--since")
    parser.add_argument("--lookback-days", type=int, default=180)
    parser.add_argument("--max-repos", type=int, default=25)
    parser.add_argument("--max-prs", type=int, default=500)
    parser.add_argument("--max-commits-per-repo", type=int, default=50)
    parser.add_argument("--repo-limit", type=int)
    parser.add_argument("--repo", action="append", dest="repos")
    parser.add_argument("--owner", action="append", dest="owners")
    parser.add_argument("--mode", choices=["prs", "commits", "all"], default="all")
    parser.add_argument("--include-forks", action="store_true")
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument("--include-inactive", action="store_true")
    parser.add_argument("--engine", choices=MODEL_ENGINES, default="heuristic")
    parser.add_argument("--provider", choices=MODEL_ENGINES)
    parser.add_argument("--allow-context-only", action="store_true")
    parser.add_argument("--max-model-prs", type=int)
    parser.add_argument("--fixture")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def paths(args: argparse.Namespace) -> tuple[Path, Path]:
    home = resolve_home(getattr(args, "home", None))
    db_path = resolve_db(home, getattr(args, "db", None))
    return home, db_path


def open_db(args: argparse.Namespace):
    home, db_path = paths(args)
    init_home(home, db_path)
    conn = connect(db_path)
    migrate(conn)
    return home, db_path, conn


def print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def cmd_init(args: argparse.Namespace) -> int:
    home, db_path = paths(args)
    init_home(home, db_path)
    print(f"Initialized prlearn at {home}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    home, db_path = paths(args)
    ensure_home(home)
    report = doctor_check(home, db_path, strict_ollama=args.strict_ollama)
    if args.json:
        print_json(report)
    elif not args.quiet:
        print("prlearn doctor")
        for key, value in report.items():
            print(f"- {key}: {value}")
        if not report["gh_auth"]["ok"]:
            print("Next step for live GitHub sync: configure a GitHub App or run `gh auth login`. Fixture sync works without GitHub auth.")
    return 0 if overall_ok(report) else 1


def client_for(args: argparse.Namespace, config: dict[str, Any]):
    if getattr(args, "fixture", None):
        return FixtureGitHubClient(Path(args.fixture))
    github = github_config(config)
    mode = getattr(args, "github_auth", None) or str(github.get("auth_mode") or "auto")
    if mode == "app":
        return github_app_client_from_config(github)
    if mode == "auto" and github_app_configured(github):
        return github_app_client_from_config(github)
    return GhCliClient()


def sync_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "author": args.author,
        "since": args.since,
        "incremental": args.incremental,
        "lookback_days": args.lookback_days,
        "limit": args.limit,
        "repos": args.repos or [],
        "owners": args.owners or [],
        "dry_run": args.dry_run,
    }


def seed_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "author": args.author,
        "since": args.since,
        "lookback_days": args.lookback_days,
        "max_repos": args.max_repos,
        "max_prs": args.max_prs,
        "max_commits_per_repo": args.max_commits_per_repo,
        "max_model_prs": args.max_model_prs,
        "repo_limit": args.repo_limit,
        "repos": args.repos or [],
        "owners": args.owners or [],
        "mode": args.mode,
        "include_forks": args.include_forks,
        "include_archived": args.include_archived,
        "include_inactive": args.include_inactive,
        "engine": args.provider or args.engine,
        "allow_context_only": args.allow_context_only,
        "dry_run": args.dry_run,
    }


def cmd_sync(args: argparse.Namespace) -> int:
    home, db_path, conn = open_db(args)
    try:
        options = sync_options(args)
        config = load_config(home, db_path)
        options["config"] = config
        stats = sync_prs(conn, client_for(args, config), options)
    except (EncryptionError, GitHubClientError) as exc:
        print(f"sync failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if args.json:
        print_json(stats)
    else:
        print(f"Synced {stats['prs_seen']} PRs; changed {stats['prs_changed']}; events {stats['events_upserted']}.")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    home, db_path, conn = open_db(args)
    try:
        options = seed_options(args)
        config = load_config(home, db_path)
        options["config"] = config
        stats = seed_account(conn, client_for(args, config), options)
    except (EncryptionError, GitHubClientError, ExtractionEngineError) as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if args.json:
        print_json(stats)
    else:
        print(f"Seeded {stats['repos_selected']} repos from {stats['repos_seen']} seen.")
        print(f"PRs seen {stats['prs_seen']}; changed {stats['prs_changed']}; events {stats['events_upserted']}.")
        print(f"Commits seen {stats['commits_seen']}; signals {stats['commit_signals']}; candidates created {stats['commit_candidates_created']}.")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    home, db_path, conn = open_db(args)
    try:
        config = load_config(home, db_path)
        engine = args.provider or args.engine
        if args.fixture:
            sync_prs(conn, FixtureGitHubClient(Path(args.fixture)), {"incremental": False, "dry_run": False, "config": config})
        stats = extract_cards(
            conn,
            all_prs=args.all,
            pr_ref=args.pr,
            min_confidence=args.min_confidence,
            engine=engine,
            config=config,
            allow_context_only=args.allow_context_only,
            model_limit=args.max_model_prs,
        )
    except (EncryptionError, ExtractionEngineError) as exc:
        print(f"extract failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if args.json:
        print_json(stats)
    else:
        print(f"Processed {stats['prs_processed']} PRs with {engine}; created {stats['created']}; updated {stats['updated']}.")
    return 0


def cmd_dedupe(args: argparse.Namespace) -> int:
    _, _, conn = open_db(args)
    try:
        stats = dedupe_cards(conn)
    finally:
        conn.close()
    if args.json:
        print_json(stats)
    else:
        print(f"Merged {stats['merged']} duplicate cards.")
    return 0


def acquire_lock(home: Path) -> Path:
    lock = home / "prlearn.lock"
    if lock.exists():
        try:
            pid_s, created = lock.read_text().split("\n", 1)
            if is_pid_alive(int(pid_s)):
                raise RuntimeError(f"another prlearn daily run is active (pid {pid_s})")
        except ValueError:
            pass
        lock.unlink(missing_ok=True)
    lock.write_text(f"{os.getpid()}\n{time.time()}\n")
    return lock


def cmd_daily(args: argparse.Namespace) -> int:
    home, db_path = paths(args)
    engine = args.provider or args.engine
    init_home(home, db_path)
    lock = acquire_lock(home)
    log_path = home / "logs" / "daily.log"
    stats: dict[str, object] = {"started_at": utcnow()}
    return_code = 0
    try:
        with log_path.open("a") as log, redirect_stdout(log), redirect_stderr(log):
            print(f"[{utcnow()}] daily start")
            doctor = doctor_check(home, db_path, strict_ollama=engine == "ollama")
            stats["doctor_ok"] = overall_ok(doctor)
            conn = connect(db_path)
            migrate(conn)
            try:
                config = load_config(home, db_path)
                try:
                    sync_stats = sync_prs(conn, client_for(args, config), {**sync_options(args), "incremental": True, "config": config})
                except (EncryptionError, GitHubClientError) as exc:
                    error = str(exc)
                    stats["error"] = error
                    print(f"[{utcnow()}] daily failed: {error}")
                    return_code = 1
                else:
                    try:
                        extract_stats = extract_cards(conn, engine=engine, config=config, allow_context_only=args.allow_context_only, model_limit=args.max_model_prs)
                    except (EncryptionError, ExtractionEngineError) as exc:
                        error = str(exc)
                        stats.update({"sync": sync_stats, "error": error})
                        print(f"[{utcnow()}] daily failed: {error}")
                        return_code = 1
                    else:
                        dedupe_stats = dedupe_cards(conn)
                        export_paths = export_all(conn, home / "exports", "all")
                        report_path = write_daily_report(conn, home / "reports", {**sync_stats, **extract_stats, **dedupe_stats})
                        summary = daily_summary(conn, {**sync_stats, **extract_stats, **dedupe_stats}, report_path)
                        stats.update({"sync": sync_stats, "extract": extract_stats, "dedupe": dedupe_stats, "exports": export_paths, "report": str(report_path), "summary": summary})
            finally:
                conn.close()
            if return_code == 0:
                print(f"[{utcnow()}] daily success")
    finally:
        lock.unlink(missing_ok=True)
    if args.json:
        print_json(stats)
    elif return_code:
        print(f"Daily failed: {stats.get('error')}")
    else:
        print(render_daily_summary(stats.get("summary") or {}))
    return return_code


def cmd_eval(args: argparse.Namespace) -> int:
    home = resolve_home(args.home) if args.home else None
    result = evaluate_fixtures(
        fixture=Path(args.fixture),
        incremental_fixture=Path(args.incremental_fixture),
        home=home,
        engine=args.engine,
        expected_cards=args.expected_cards,
        expected_null_recurrence=args.expected_null_recurrence,
        min_events=args.min_events,
        expected_titles=args.expected_titles or DEFAULT_EXPECTED_TITLES,
        keep_home=args.keep_home,
    )
    if args.json:
        print_json(result)
    else:
        print(f"prlearn eval: {'passed' if result['passed'] else 'failed'}")
        for item in result["checks"]:
            mark = "ok" if item["ok"] else "fail"
            print(f"- {mark}: {item['name']} expected={item['expected']} actual={item['actual']}")
        if result.get("home"):
            print(f"Home: {result['home']}")
    return 0 if result["passed"] else 1


def cmd_compare(args: argparse.Namespace) -> int:
    home = resolve_home(args.home) if args.home else None
    engines = [item.strip() for item in str(args.engines).split(",") if item.strip()]
    result = compare_engines(
        fixture=Path(args.fixture),
        incremental_fixture=Path(args.incremental_fixture) if args.incremental_fixture else None,
        home=home,
        engines=engines,
        keep_home=args.keep_home,
        allow_context_only=args.allow_context_only,
    )
    if args.json:
        print_json(result)
    else:
        print(f"prlearn compare: {'passed' if result['passed'] else 'partial'}")
        for item in result["engines"]:
            status = "ok" if item["ok"] else "fail"
            cards = len(item.get("cards") or [])
            candidates = len(item.get("candidates") or [])
            print(f"- {status}: {item['engine']} prs={item.get('prs_seen', 0)} cards={cards} candidates={candidates}")
            if item.get("error"):
                print(f"  error: {item['error']}")
            for output in (item.get("cards") or []) + (item.get("candidates") or []):
                print(f"  - {output['title']} ({output.get('source', 'heuristic')})")
        if result.get("home"):
            print(f"Home: {result['home']}")
    return 0 if result["passed"] else 1


def cmd_compare_live(args: argparse.Namespace) -> int:
    home, db_path = paths(args)
    engines = [item.strip() for item in str(args.engines).split(",") if item.strip()]
    try:
        result = compare_live_engines(
            base_home=home,
            base_db_path=db_path,
            engines=engines,
            seed_options=seed_options(args),
            github_auth=args.github_auth,
            keep_home=args.keep_home,
        )
    except (EncryptionError, GitHubClientError, ExtractionEngineError) as exc:
        print(f"compare-live failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print_json(result)
    else:
        print(f"prlearn compare-live: {'passed' if result['passed'] else 'partial'}")
        for item in result["engines"]:
            status = "ok" if item["ok"] else "fail"
            candidates = len(item.get("candidates") or [])
            print(f"- {status}: {item['engine']} repos={item.get('repos_selected', 0)} prs={item.get('prs_seen', 0)} candidates={candidates}")
            if item.get("error"):
                print(f"  error: {item['error']}")
            for candidate in item.get("candidates") or []:
                print(f"  - {candidate['title']} ({candidate.get('source')})")
        if result.get("home"):
            print(f"Home: {result['home']}")
    return 0 if result["passed"] else 1


def cmd_list(args: argparse.Namespace) -> int:
    _, _, conn = open_db(args)
    try:
        status = args.status
        for candidate_status in ["accepted", "pending", "rejected", "archived"]:
            if getattr(args, candidate_status, False):
                status = candidate_status
        rows = learning_rows(conn, status=status, tag=args.tag, repo=args.repo, limit=args.limit)
        cards = [card_summary(row_to_dict(conn, row), verbose=args.verbose) for row in rows]
    finally:
        conn.close()
    if args.json:
        print_json(cards)
    else:
        for card in cards:
            print(f"{card['id']}: {card['title']} [{card['status']}] seen {card['recurrence_count']}x score={card['score']}")
            print(f"   Action: {card['suggested_action']}")
            print(f"   Tags: {', '.join(card['tags'])}")
            if args.verbose:
                print(f"   Prevention: {card['prevention_rule']}")
                for evidence in card["evidence"]:
                    ref = f"{evidence.get('repo_full_name')}#{evidence.get('pr_number')}"
                    quote = f": {evidence.get('quote')}" if evidence.get("quote") else ""
                    print(f"   Evidence: {ref}{quote}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    _, _, conn = open_db(args)
    try:
        candidate_items = [candidate_summary(candidate_to_dict(conn, row)) for row in candidate_rows(conn, status="pending", limit=args.limit)]
        if args.min_score is not None:
            candidate_items = [item for item in candidate_items if readiness_score(item) >= args.min_score]
        if args.ready:
            candidate_items = [item for item in candidate_items if item["ready"]]
        pending_cards = [card_summary(row_to_dict(conn, row), verbose=True) for row in learning_rows(conn, status="pending", limit=args.limit)]
        if args.json:
            print_json({"candidates": candidate_items, "pending_cards": [] if args.candidates else pending_cards})
            return 0
        if args.candidates:
            for candidate in candidate_items:
                print_review_candidate(candidate)
            return 0
        for candidate in candidate_items:
            print_review_candidate(candidate)
            choice = input("[a]ccept [r]eject [m]erge [s]kip: ").strip().lower()
            if choice == "a":
                accept_learning_candidate(conn, str(candidate["id"]))
            elif choice == "r":
                reject_learning_candidate(conn, str(candidate["id"]))
            elif choice == "m":
                target = int(input("Merge into card ID: ").strip())
                merge_learning_candidate(conn, str(candidate["id"]), target)
        if args.candidates:
            return 0
        for card in pending_cards:
            print(f"\n{card['id']}: {card['title']}\n{card['lesson']}\nPrevention: {card['prevention_rule']}")
            choice = input("[a]ccept [r]eject [e]dit [m]erge [s]kip: ").strip().lower()
            if choice == "a":
                update_status(conn, int(card["id"]), "accepted")
            elif choice == "r":
                update_status(conn, int(card["id"]), "rejected")
            elif choice == "e":
                edit_card(conn, int(card["id"]))
            elif choice == "m":
                target = int(input("Merge into card ID: ").strip())
                merge_cards(conn, int(card["id"]), target)
    finally:
        conn.close()
    return 0


def print_review_candidate(candidate: dict[str, Any]) -> None:
    print(f"\nCandidate {candidate['id']}: {candidate['title']}")
    print(f"Confidence: {candidate['confidence']}  Evidence: {candidate['evidence_count']}  Suggested action: {candidate['suggested_action']}")
    print(f"Lesson: {candidate['lesson']}")
    print(f"Prevention: {candidate['prevention_rule']}")
    for evidence in candidate.get("evidence") or []:
        ref = f"{evidence.get('repo_full_name')}#{evidence.get('pr_number')}"
        quote = f": {evidence.get('quote')}" if evidence.get("quote") else ""
        print(f"Evidence: {ref}{quote}")


def edit_card(conn, card_id: int) -> None:
    row = conn.execute("select * from learning_cards where id=?", (card_id,)).fetchone()
    if not row:
        return
    title = input(f"Title [{row['title']}]: ").strip() or row["title"]
    lesson = input(f"Lesson [{row['lesson']}]: ").strip() or row["lesson"]
    prevention = input(f"Prevention [{row['prevention_rule']}]: ").strip() or row["prevention_rule"]
    conn.execute("update learning_cards set title=?, lesson=?, prevention_rule=?, updated_at=? where id=?", (title, lesson, prevention, utcnow(), card_id))
    conn.commit()


def cmd_accept(args: argparse.Namespace) -> int:
    return status_command(args, "accepted")


def cmd_reject(args: argparse.Namespace) -> int:
    return status_command(args, "rejected")


def cmd_archive(args: argparse.Namespace) -> int:
    return status_command(args, "archived")


def status_command(args: argparse.Namespace, status: str) -> int:
    _, _, conn = open_db(args)
    try:
        ok = update_status(conn, args.id, status)
    finally:
        conn.close()
    print(f"{status} {args.id}" if ok else f"card not found: {args.id}")
    return 0 if ok else 1


def cmd_merge(args: argparse.Namespace) -> int:
    _, _, conn = open_db(args)
    try:
        ok = merge_cards(conn, args.source_id, args.target_id)
    finally:
        conn.close()
    print(f"merged {args.source_id} into {args.target_id}" if ok else "merge failed")
    return 0 if ok else 1


def cmd_accept_candidate(args: argparse.Namespace) -> int:
    _, _, conn = open_db(args)
    try:
        ok = accept_learning_candidate(conn, args.id)
    finally:
        conn.close()
    print(f"accepted candidate {args.id}" if ok else f"candidate not accepted: {args.id}")
    return 0 if ok else 1


def cmd_reject_candidate(args: argparse.Namespace) -> int:
    _, _, conn = open_db(args)
    try:
        ok = reject_learning_candidate(conn, args.id, args.reason)
    finally:
        conn.close()
    print(f"rejected candidate {args.id}" if ok else f"candidate not found: {args.id}")
    return 0 if ok else 1


def cmd_merge_candidate(args: argparse.Namespace) -> int:
    _, _, conn = open_db(args)
    try:
        ok = merge_learning_candidate(conn, args.id, args.card_id)
    finally:
        conn.close()
    print(f"merged candidate {args.id} into card {args.card_id}" if ok else "candidate merge failed")
    return 0 if ok else 1


def cmd_telegram_status(args: argparse.Namespace) -> int:
    _, _, conn = open_db(args)
    try:
        pending = len(candidate_rows(conn, status="pending"))
    finally:
        conn.close()
    status = {
        "token_env": "PRLEARN_TELEGRAM_BOT_TOKEN",
        "chat_id_env": "PRLEARN_TELEGRAM_CHAT_ID",
        "token_configured": bool(os.environ.get("PRLEARN_TELEGRAM_BOT_TOKEN")),
        "chat_id_configured": bool(os.environ.get("PRLEARN_TELEGRAM_CHAT_ID")),
        "pending_candidates": pending,
    }
    if args.json:
        print_json(status)
    else:
        print(f"Telegram token configured: {status['token_configured']}")
        print(f"Telegram chat configured: {status['chat_id_configured']}")
        print(f"Pending candidates: {pending}")
    return 0


def cmd_telegram_send(args: argparse.Namespace) -> int:
    home, db_path, conn = open_db(args)
    try:
        if args.dry_run:
            stats = send_pending_candidates(conn, TelegramClient("dry-run"), chat_id="dry-run", limit=args.limit, ready=args.ready, dry_run=True)
        else:
            settings = settings_from_env()
            client = TelegramClient(settings.token)
            stats = send_pending_candidates(conn, client, chat_id=settings.chat_id, limit=args.limit, ready=args.ready, dry_run=False)
    except TelegramError as exc:
        print(f"telegram send failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if args.json:
        print_json(stats)
    else:
        print(f"sent={stats['sent']} candidates={stats['candidates']} dry_run={bool(stats['dry_run'])}")
    return 0


def cmd_telegram_chats(args: argparse.Namespace) -> int:
    try:
        client = TelegramClient(token_from_env(), timeout=max(args.timeout + 10, 30))
        chats = discover_chats(client, timeout=args.timeout, limit=args.limit)
    except TelegramError as exc:
        print(f"telegram chats failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print_json({"chats": chats})
    else:
        if not chats:
            print("No chats found. Open Telegram, message your bot, then run this again.")
        for chat in chats:
            title = f" {chat['title']}" if chat.get("title") else ""
            print(f"{chat['id']} {chat.get('type') or 'unknown'}{title}")
    return 0


def cmd_telegram_poll(args: argparse.Namespace) -> int:
    _, _, conn = open_db(args)
    try:
        settings = settings_from_env()
        client = TelegramClient(settings.token, timeout=max(args.timeout + 10, 30))
        stats = poll_ratings(conn, client, chat_id=settings.chat_id, timeout=args.timeout, limit=args.limit, delete_reviewed=not args.keep_reviewed)
    except TelegramError as exc:
        print(f"telegram poll failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if args.json:
        print_json(stats)
    else:
        print(f"updates={stats['updates']} processed={stats['processed']} ignored={stats['ignored']} deleted={stats['deleted']}")
    return 0


def cmd_telegram_rate(args: argparse.Namespace) -> int:
    _, _, conn = open_db(args)
    try:
        result = apply_candidate_rating(conn, candidate_id=args.candidate_id, rating=args.rating)
    finally:
        conn.close()
    if args.json:
        print_json(result)
    else:
        print(f"{result['action']} {args.candidate_id} as {args.rating}")
    return 0 if result["ok"] else 1


def cmd_privacy_status(args: argparse.Namespace) -> int:
    home, db_path, conn = open_db(args)
    try:
        config = load_config(home, db_path)
        status = raw_json_status(conn)
        status["raw_json_encryption_enabled"] = bool((config.get("privacy") or {}).get("encrypt_raw_json"))
        status["passphrase_env"] = str((config.get("privacy") or {}).get("passphrase_env") or "PRLEARN_PASSPHRASE")
    finally:
        conn.close()
    if args.json:
        print_json(status)
    else:
        print(f"Raw JSON encryption enabled: {status['raw_json_encryption_enabled']}")
        print(f"Passphrase env: {status['passphrase_env']}")
        for item in status["tables"]:
            print(f"- {item['table']}.{item['column']}: encrypted={item['encrypted']} plain={item['plain']} empty={item['empty']}")
    return 0


def cmd_privacy_encrypt_raw(args: argparse.Namespace) -> int:
    home, db_path, conn = open_db(args)
    try:
        config = load_config(home, db_path)
        try:
            stats = encrypt_raw_json(conn, config, prompt=args.prompt_passphrase)
        except EncryptionError as exc:
            print(f"privacy encrypt-raw failed: {exc}", file=sys.stderr)
            return 1
        config.setdefault("privacy", {})["encrypt_raw_json"] = True
        save_config(home, config)
    finally:
        conn.close()
    if args.json:
        print_json(stats)
    else:
        print(f"encrypted={stats['encrypted']} skipped={stats['skipped']} empty={stats['empty']}")
    return 0


def cmd_privacy_decrypt_raw(args: argparse.Namespace) -> int:
    home, db_path, conn = open_db(args)
    try:
        config = load_config(home, db_path)
        try:
            stats = decrypt_raw_json(conn, config, prompt=args.prompt_passphrase)
        except EncryptionError as exc:
            print(f"privacy decrypt-raw failed: {exc}", file=sys.stderr)
            return 1
        config.setdefault("privacy", {})["encrypt_raw_json"] = False
        save_config(home, config)
    finally:
        conn.close()
    if args.json:
        print_json(stats)
    else:
        print(f"decrypted={stats['decrypted']} skipped={stats['skipped']} empty={stats['empty']}")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    _, _, conn = open_db(args)
    try:
        path_args = list(args.paths or [])
        for group in args.paths_option or []:
            path_args.extend(group)
        report = preflight_report(
            conn,
            Path(args.repo),
            task=args.task,
            paths=path_args,
            top=args.top,
            include_pending=args.include_pending,
            verbose=args.verbose,
        )
    finally:
        conn.close()
    if args.json:
        print_json(report)
    else:
        print(render_preflight(report, verbose=args.verbose))
    return 0


def cmd_insights(args: argparse.Namespace) -> int:
    _, _, conn = open_db(args)
    try:
        report = actionable_insights_report(conn) if args.actionable else usefulness_report(conn)
    finally:
        conn.close()
    if args.json:
        print_json(report)
    else:
        print(render_actionable_insights_report(report) if args.actionable else render_usefulness_report(report))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    home, _, conn = open_db(args)
    try:
        written = export_all(conn, Path(args.out).expanduser() if args.out else home / "exports", args.format, include_pending=args.include_pending)
    finally:
        conn.close()
    print_json(written)
    return 0


def cmd_schedule_install(args: argparse.Namespace) -> int:
    home, _ = paths(args)
    ensure_home(home)
    spec = install_schedule(home)
    print_json(spec) if args.json else print(spec["install"])
    return 0


def cmd_schedule_status(args: argparse.Namespace) -> int:
    home, _ = paths(args)
    spec = schedule_status(home)
    print_json(spec) if args.json else print(spec)
    return 0


def cmd_schedule_uninstall(args: argparse.Namespace) -> int:
    home, _ = paths(args)
    removed = uninstall_schedule(home)
    print_json({"removed": removed}) if args.json else print(f"removed={removed}")
    return 0


def cmd_schedule_print(args: argparse.Namespace) -> int:
    home, _ = paths(args)
    ensure_home(home)
    spec = schedule_text(home)
    print_json(spec) if args.json else print(spec["content"])
    return 0
