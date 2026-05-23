from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from prlearn.cli import main
from prlearn.crypto import ENCRYPTED_PREFIX
from prlearn.evidence import ensure_evidence_for_dirty_prs
from prlearn.usefulness import rank_cards
from prlearn.util import utcnow


ROOT = Path(__file__).resolve().parents[1]
SMALL = ROOT / "tests" / "fixtures" / "github_small.json"
INCREMENTAL = ROOT / "tests" / "fixtures" / "github_incremental.json"


def run_cli(*args: str) -> int:
    return main(list(args))


def connect(home: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(home / "prlearn.db")
    conn.row_factory = sqlite3.Row
    return conn


def accept_card(home: Path, mistake_pattern: str) -> int:
    conn = connect(home)
    try:
        card_id = conn.execute("select id from learning_cards where mistake_pattern=?", (mistake_pattern,)).fetchone()["id"]
    finally:
        conn.close()
    assert run_cli("accept", str(card_id), "--home", str(home)) == 0
    return int(card_id)


def make_git_repo(path: Path, remote: str = "https://github.com/octo/app.git", changed_file: str = "src/dashboard.ts") -> Path:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True, capture_output=True, text=True)
    file_path = path / changed_file
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("export const value = profile.name;\n")
    subprocess.run(["git", "add", changed_file], cwd=path, check=True, capture_output=True, text=True)
    return file_path


def insert_generic_card(home: Path, *, status: str = "accepted", contexts: list[str] | None = None) -> int:
    conn = connect(home)
    now = utcnow()
    try:
        card_id = conn.execute(
            """
            insert into learning_cards(canonical_key, title, lesson, mistake_pattern, prevention_rule, tags_json,
              contexts_json, severity, confidence, status, recurrence_count, first_seen_at, last_seen_at, created_at, updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"generic-tests-{status}",
                "Add tests",
                "Add tests.",
                "generic-tests",
                "Add tests.",
                json.dumps(["tests"]),
                json.dumps(contexts or ["octo/app"]),
                5,
                0.95,
                status,
                10,
                now,
                now,
                now,
                now,
            ),
        ).lastrowid
        conn.commit()
        return int(card_id)
    finally:
        conn.close()


def assert_no_raw_identifiers(text: str, *, extra_forbidden: list[str] | None = None) -> None:
    forbidden = [
        "octo/app",
        "octo/payments",
        "https://github.com",
        "src/Profile.tsx",
        "src/dashboard.ts",
        "api/webhook.ts",
        "profile-settings",
    ]
    for value in forbidden + (extra_forbidden or []):
        assert value not in text


def test_init_is_idempotent(tmp_path: Path) -> None:
    assert run_cli("init", "--home", str(tmp_path)) == 0
    assert run_cli("init", "--home", str(tmp_path)) == 0
    assert (tmp_path / "config.json").exists()
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "reports").is_dir()
    assert (tmp_path / "exports").is_dir()
    conn = connect(tmp_path)
    assert conn.execute("select version from schema_migrations").fetchone()["version"] == 1


def test_daily_fixture_idempotent_incremental_dedupe_and_exports(tmp_path: Path) -> None:
    assert run_cli("daily", "--home", str(tmp_path), "--fixture", str(SMALL), "--json") == 0
    conn = connect(tmp_path)
    first_cards = conn.execute("select count(*) as count from learning_cards").fetchone()["count"]
    first_events = conn.execute("select count(*) as count from events").fetchone()["count"]
    assert first_cards == 3
    assert first_events >= 7
    conn.close()

    assert run_cli("daily", "--home", str(tmp_path), "--fixture", str(SMALL), "--json") == 0
    conn = connect(tmp_path)
    assert conn.execute("select count(*) as count from learning_cards").fetchone()["count"] == first_cards
    assert conn.execute("select count(*) as count from events").fetchone()["count"] == first_events
    null_card = conn.execute("select * from learning_cards where mistake_pattern='tests-null-state'").fetchone()
    assert null_card["recurrence_count"] == 2
    conn.close()

    assert run_cli("daily", "--home", str(tmp_path), "--fixture", str(INCREMENTAL), "--json") == 0
    conn = connect(tmp_path)
    assert conn.execute("select count(*) as count from learning_cards").fetchone()["count"] == first_cards
    null_card = conn.execute("select * from learning_cards where mistake_pattern='tests-null-state'").fetchone()
    assert null_card["recurrence_count"] == 3
    evidence = conn.execute("select count(*) as count from learning_evidence where learning_id=?", (null_card["id"],)).fetchone()["count"]
    assert evidence == 3
    assert conn.execute("select count(*) as count from events where kind='pr_body'").fetchone()["count"] == 1
    assert conn.execute("select count(*) as count from events where kind='timeline_ready_for_review'").fetchone()["count"] == 1
    assert conn.execute("select count(*) as count from events where kind='check_annotation'").fetchone()["count"] == 1
    file_row = conn.execute("select raw_json from pr_files where path='src/dashboard.ts'").fetchone()
    assert "patch" in file_row["raw_json"]
    assert (tmp_path / "exports" / "LEARNINGS.md").exists()
    assert (tmp_path / "exports" / "context.json").exists()
    assert (tmp_path / "exports" / "rules.md").exists()
    assert list((tmp_path / "reports").glob("*.md"))


def test_fixture_sync_respects_explicit_since(tmp_path: Path, capsys) -> None:
    assert run_cli("sync", "--home", str(tmp_path), "--fixture", str(SMALL), "--since", "2026-05-05T00:00:00Z", "--json") == 0
    sync_stats = json.loads(capsys.readouterr().out)
    assert sync_stats["since"] == "2026-05-05T00:00:00Z"
    assert sync_stats["prs_seen"] == 2


def test_raw_json_encryption_locks_and_reveals_fixture_payloads(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("PRLEARN_PASSPHRASE", "fixture-passphrase")
    assert run_cli("sync", "--home", str(tmp_path), "--fixture", str(SMALL), "--json") == 0
    initial_sync = json.loads(capsys.readouterr().out)
    assert initial_sync["prs_seen"] == 4

    conn = connect(tmp_path)
    first_events = conn.execute("select count(*) as count from events").fetchone()["count"]
    plain_file = conn.execute("select raw_json from pr_files where path='src/dashboard.ts'").fetchone()["raw_json"]
    assert "patch" in plain_file
    assert not plain_file.startswith(ENCRYPTED_PREFIX)
    conn.close()

    assert run_cli("privacy", "encrypt-raw", "--home", str(tmp_path), "--json") == 0
    encrypted_result = json.loads(capsys.readouterr().out)
    assert encrypted_result["encrypted"] > 0

    assert run_cli("privacy", "status", "--home", str(tmp_path), "--json") == 0
    status = json.loads(capsys.readouterr().out)
    assert status["raw_json_encryption_enabled"] is True
    assert status["totals"]["encrypted"] > 0
    assert status["totals"]["plain"] == 0

    config = json.loads((tmp_path / "config.json").read_text())
    conn = connect(tmp_path)
    encrypted_file = conn.execute("select raw_json from pr_files where path='src/dashboard.ts'").fetchone()["raw_json"]
    assert encrypted_file.startswith(ENCRYPTED_PREFIX)
    assert "patch" not in encrypted_file
    evidence_stats = ensure_evidence_for_dirty_prs(conn, all_prs=True, config=config)
    assert evidence_stats["evidence_chunks_created"] > 0
    conn.close()

    assert run_cli("sync", "--home", str(tmp_path), "--fixture", str(SMALL), "--json") == 0
    second_sync = json.loads(capsys.readouterr().out)
    assert second_sync["prs_changed"] == 0
    assert second_sync["events_upserted"] == 0
    conn = connect(tmp_path)
    assert conn.execute("select count(*) as count from events").fetchone()["count"] == first_events
    conn.close()

    assert run_cli("privacy", "decrypt-raw", "--home", str(tmp_path), "--json") == 0
    decrypted_result = json.loads(capsys.readouterr().out)
    assert decrypted_result["decrypted"] > 0

    assert run_cli("privacy", "status", "--home", str(tmp_path), "--json") == 0
    unlocked_status = json.loads(capsys.readouterr().out)
    assert unlocked_status["raw_json_encryption_enabled"] is False
    assert unlocked_status["totals"]["encrypted"] == 0

    conn = connect(tmp_path)
    decrypted_file = conn.execute("select raw_json from pr_files where path='src/dashboard.ts'").fetchone()["raw_json"]
    assert "patch" in decrypted_file
    assert not decrypted_file.startswith(ENCRYPTED_PREFIX)
    conn.close()


def test_raw_json_encryption_requires_passphrase(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.delenv("PRLEARN_PASSPHRASE", raising=False)
    assert run_cli("sync", "--home", str(tmp_path), "--fixture", str(SMALL), "--json") == 0
    capsys.readouterr()

    assert run_cli("privacy", "encrypt-raw", "--home", str(tmp_path), "--json") == 1
    captured = capsys.readouterr()
    assert "missing passphrase" in captured.err


def test_accept_preflight_and_json_export(tmp_path: Path, capsys) -> None:
    assert run_cli("daily", "--home", str(tmp_path), "--fixture", str(SMALL)) == 0
    conn = connect(tmp_path)
    card_id = conn.execute("select id from learning_cards where mistake_pattern='webhook-signature'").fetchone()["id"]
    conn.close()
    assert run_cli("accept", str(card_id), "--home", str(tmp_path)) == 0
    assert run_cli("preflight", "--home", str(tmp_path), "--task", "build a Stripe webhook handler", "--top", "2") == 0
    out = capsys.readouterr().out
    assert "Verify webhook signatures" in out
    out_dir = tmp_path / "out"
    assert run_cli("export", "--home", str(tmp_path), "--out", str(out_dir), "--format", "json") == 0
    data = json.loads((out_dir / "context.json").read_text())
    assert data["learnings"]


def test_preflight_uses_git_changed_paths_json_and_verbose_output(tmp_path: Path, capsys) -> None:
    assert run_cli("daily", "--home", str(tmp_path), "--fixture", str(SMALL), "--json") == 0
    accept_card(tmp_path, "typecheck-before-pr")
    capsys.readouterr()
    repo = tmp_path / "repo"
    repo.mkdir()
    make_git_repo(repo, changed_file="src/dashboard.ts")

    assert run_cli("preflight", "--home", str(tmp_path), "--repo", str(repo), "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["context"]["git_detected"] is True
    assert result["context"]["repo"].startswith("[REDACTED_REPO_")
    assert result["context"]["changed_files"][0].startswith("[REDACTED_PATH_")
    assert result["lessons"][0]["card"]["title"] == "Run typecheck and build checks before opening PRs"
    assert result["lessons"][0]["applies_because"]

    assert run_cli("preflight", "--home", str(tmp_path), "--repo", str(repo), "--verbose") == 0
    out = capsys.readouterr().out
    assert "Relevant prior lessons for this work" in out
    assert "Evidence:" in out


def test_preflight_path_filter_and_no_git_fallback(tmp_path: Path, capsys) -> None:
    assert run_cli("daily", "--home", str(tmp_path), "--fixture", str(SMALL), "--json") == 0
    accept_card(tmp_path, "webhook-signature")
    capsys.readouterr()

    no_git = tmp_path / "not-a-repo"
    no_git.mkdir()
    assert run_cli("preflight", "--home", str(tmp_path), "--repo", str(no_git), "--paths", "src/webhook.ts", "--task", "Stripe webhook handler", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["context"]["git_detected"] is False
    assert result["context"]["paths"][0].startswith("[REDACTED_PATH_")
    assert result["lessons"][0]["card"]["title"] == "Verify webhook signatures before trusting payloads"

    assert run_cli("preflight", "src/webhook.ts", "--home", str(tmp_path), "--repo", str(no_git), "--task", "Stripe webhook handler", "--json") == 0
    positional = json.loads(capsys.readouterr().out)
    assert positional["context"]["paths"][0].startswith("[REDACTED_PATH_")
    assert positional["lessons"][0]["card"]["title"] == "Verify webhook signatures before trusting payloads"


def test_preflight_ranks_specific_lessons_above_generic_but_can_show_generic(tmp_path: Path, capsys) -> None:
    assert run_cli("daily", "--home", str(tmp_path), "--fixture", str(SMALL), "--json") == 0
    accept_card(tmp_path, "tests-null-state")
    insert_generic_card(tmp_path)
    capsys.readouterr()
    repo = tmp_path / "repo"
    repo.mkdir()
    make_git_repo(repo, changed_file="src/profile.tsx")

    assert run_cli("preflight", "--home", str(tmp_path), "--repo", str(repo), "--paths", "src/profile.tsx", "--task", "null UI tests", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["lessons"][0]["card"]["title"] == "Cover null and empty states with regression tests"
    assert any(item["card"]["title"] == "Add tests" for item in result["lessons"])

    generic_home = tmp_path / "generic-only"
    assert run_cli("init", "--home", str(generic_home)) == 0
    insert_generic_card(generic_home)
    capsys.readouterr()
    assert run_cli("preflight", "--home", str(generic_home), "--repo", str(repo), "--task", "tests", "--json") == 0
    generic_result = json.loads(capsys.readouterr().out)
    assert generic_result["lessons"][0]["card"]["title"] == "Add tests"


def test_usefulness_ranking_prefers_recurrent_card_when_otherwise_similar() -> None:
    base = {
        "id": 1,
        "title": "Validate API error paths before merging",
        "lesson": "Review feedback showed API error paths need explicit validation before merge.",
        "prevention_rule": "Before opening API PRs, add regression tests for non-2xx responses and error payload shape.",
        "mistake_pattern": "api-error-paths",
        "tags": ["api", "tests", "validation"],
        "contexts": ["octo/app"],
        "severity": 3,
        "confidence": 0.8,
        "status": "accepted",
        "first_seen_at": "2026-05-01T00:00:00Z",
        "last_seen_at": "2026-05-01T00:00:00Z",
        "evidence": [{"repo_full_name": "octo/app", "pr_number": 1, "path": "src/api/orders.ts", "quote": "error response needs coverage"}],
    }
    one_off = {**base, "id": 1, "recurrence_count": 1}
    recurrent = {**base, "id": 2, "title": "Validate API error paths before merging again", "recurrence_count": 5}
    ranked = rank_cards([one_off, recurrent], {"repo": "octo/app", "paths": ["src/api/orders.ts"]}, top=2)
    assert ranked[0]["card"]["id"] == 2


def test_insights_and_review_json_are_actionable(tmp_path: Path, capsys) -> None:
    assert run_cli("daily", "--home", str(tmp_path), "--fixture", str(SMALL), "--json") == 0
    accept_card(tmp_path, "tests-null-state")
    insert_generic_card(tmp_path)
    capsys.readouterr()

    assert run_cli("insights", "--home", str(tmp_path), "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["accepted_lessons"] == 2
    assert report["likely_generic_lessons"] == 1
    assert "suggested_next_commands" in report

    assert run_cli("insights", "--home", str(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "prlearn usefulness report" in out
    assert "Suggested next commands" in out

    assert run_cli("review", "--home", str(tmp_path), "--json", "--limit", "2") == 0
    review = json.loads(capsys.readouterr().out)
    assert review["pending_cards"]
    assert "suggested_action" in review["pending_cards"][0]


def test_actionable_insights_route_rules_and_feedback(tmp_path: Path, capsys) -> None:
    assert run_cli("daily", "--home", str(tmp_path), "--fixture", str(SMALL), "--json") == 0
    card_id = accept_card(tmp_path, "tests-null-state")
    insert_generic_card(tmp_path)
    conn = connect(tmp_path)
    try:
        conn.execute(
            "insert into learning_card_feedback(learning_id, action, reason, created_at) values(?,?,?,?)",
            (card_id, "telegram_major", None, utcnow()),
        )
        conn.commit()
    finally:
        conn.close()
    capsys.readouterr()

    assert run_cli("insights", "--home", str(tmp_path), "--actionable", "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["do_before_next_pr"] >= 1
    assert report["summary"]["needs_human_review"] >= 1
    assert report["summary"]["low_value"] >= 1
    top = report["do_before_next_pr"][0]
    assert top["actionability"]["label"] == "major"
    assert top["rule"]["when"].startswith("When working on")
    assert top["rule"]["do"]
    assert report["feedback_loop"]["review_actions"]["telegram_major"] == 1
    assert "prlearn review --limit 10" in report["suggested_next_commands"]

    assert run_cli("insights", "--home", str(tmp_path), "--actionable") == 0
    out = capsys.readouterr().out
    assert "prlearn actionable insights" in out
    assert "Do before next PR" in out


def test_export_memory_is_concise_and_filters_unaccepted_cards(tmp_path: Path) -> None:
    assert run_cli("daily", "--home", str(tmp_path), "--fixture", str(SMALL), "--json") == 0
    accept_card(tmp_path, "webhook-signature")
    rejected_id = accept_card(tmp_path, "typecheck-before-pr")
    assert run_cli("reject", str(rejected_id), "--home", str(tmp_path)) == 0
    out_dir = tmp_path / "memory"

    assert run_cli("export", "--home", str(tmp_path), "--out", str(out_dir), "--format", "markdown") == 0
    memory = (out_dir / "LEARNINGS.md").read_text()
    assert memory.startswith("# prlearn memory")
    assert "Generated:" in memory
    assert "Verify webhook signatures before trusting payloads" in memory
    assert "Run typecheck and build checks before opening PRs" not in memory
    assert "Evidence:" not in memory
    assert "Checklist:" in memory


def test_public_export_preflight_report_and_logs_redact_metadata_surfaces(tmp_path: Path, capsys) -> None:
    assert run_cli("daily", "--home", str(tmp_path), "--fixture", str(SMALL), "--json") == 0
    accept_card(tmp_path, "tests-null-state")
    accept_card(tmp_path, "webhook-signature")
    capsys.readouterr()

    out_dir = tmp_path / "privacy-export"
    assert run_cli("export", "--home", str(tmp_path), "--out", str(out_dir), "--format", "all") == 0
    for name in ["context.json", "LEARNINGS.md", "rules.md"]:
        content = (out_dir / name).read_text()
        assert_no_raw_identifiers(content, extra_forbidden=["coder", "reviewer"])
    context = json.loads((out_dir / "context.json").read_text())
    evidence = context["learnings"][0]["evidence"][0]
    assert "repo_full_name" not in evidence
    assert "pr_number" not in evidence
    assert "url" not in evidence
    assert "path" not in evidence
    assert evidence["source_ref"].startswith("[REDACTED_EVIDENCE_")
    if evidence.get("check_name"):
        assert evidence["check_name"].startswith("[REDACTED_CHECK_")
    capsys.readouterr()

    repo = tmp_path / "repo"
    repo.mkdir()
    make_git_repo(repo, changed_file="src/dashboard.ts")
    subprocess.run(["git", "checkout", "-b", "profile-settings"], cwd=repo, check=True, capture_output=True, text=True)
    assert run_cli("preflight", "--home", str(tmp_path), "--repo", str(repo), "--verbose", "--json") == 0
    preflight_json = capsys.readouterr().out
    assert_no_raw_identifiers(preflight_json, extra_forbidden=["coder", "reviewer"])
    preflight = json.loads(preflight_json)
    assert preflight["context"]["branch"].startswith("[REDACTED_BRANCH_")
    assert preflight["lessons"][0]["evidence"][0]["source_ref"].startswith("[REDACTED_EVIDENCE_")

    assert run_cli("preflight", "--home", str(tmp_path), "--repo", str(repo), "--verbose") == 0
    assert_no_raw_identifiers(capsys.readouterr().out, extra_forbidden=["coder", "reviewer"])

    report_text = "\n".join(path.read_text() for path in (tmp_path / "reports").glob("*.md"))
    assert_no_raw_identifiers(report_text, extra_forbidden=["coder", "reviewer"])
    assert_no_raw_identifiers((tmp_path / "logs" / "daily.log").read_text(), extra_forbidden=["coder", "reviewer"])


def test_daily_text_summary_includes_next_action(tmp_path: Path, capsys) -> None:
    assert run_cli("daily", "--home", str(tmp_path), "--fixture", str(SMALL)) == 0
    out = capsys.readouterr().out
    assert "Synced PRs:" in out
    assert "Needs review:" in out
    assert "Next:" in out


def test_schedule_print_does_not_install_real_schedule(tmp_path: Path, capsys) -> None:
    assert run_cli("schedule", "print", "--home", str(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "prlearn-daily" in out
    assert (tmp_path / "bin" / "prlearn-daily").exists()


def test_eval_command_reports_fixture_quality_metrics(tmp_path: Path, capsys) -> None:
    eval_home = tmp_path / "eval"
    assert run_cli(
        "eval",
        "--home",
        str(eval_home),
        "--fixture",
        str(SMALL),
        "--incremental-fixture",
        str(INCREMENTAL),
        "--json",
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["passed"] is True
    assert result["metrics"]["card_count"] == 3
    assert result["metrics"]["second_cards_created"] == 0
    assert result["metrics"]["second_events_upserted"] == 0
    assert result["metrics"]["null_state_recurrence"] == 3
    assert result["metrics"]["exports_exist"] is True

    assert run_cli(
        "eval",
        "--home",
        str(tmp_path / "failed-eval"),
        "--fixture",
        str(SMALL),
        "--incremental-fixture",
        str(INCREMENTAL),
        "--expected-cards",
        "99",
        "--json",
    ) == 1


def test_eval_and_daily_fixture_sequence_agree_on_recurrence(tmp_path: Path, capsys) -> None:
    daily_home = tmp_path / "daily"
    assert run_cli("daily", "--home", str(daily_home), "--fixture", str(SMALL), "--json") == 0
    capsys.readouterr()
    assert run_cli("daily", "--home", str(daily_home), "--fixture", str(SMALL), "--json") == 0
    capsys.readouterr()
    assert run_cli("daily", "--home", str(daily_home), "--fixture", str(INCREMENTAL), "--json") == 0
    capsys.readouterr()

    conn = connect(daily_home)
    try:
        daily_metrics = {
            "card_count": conn.execute("select count(*) as count from learning_cards").fetchone()["count"],
            "event_count": conn.execute("select count(*) as count from events").fetchone()["count"],
            "null_state_recurrence": conn.execute("select recurrence_count from learning_cards where mistake_pattern='tests-null-state'").fetchone()["recurrence_count"],
            "null_state_evidence": conn.execute(
                """
                select count(*) as count
                from learning_evidence le
                join learning_cards lc on lc.id = le.learning_id
                where lc.mistake_pattern='tests-null-state'
                """
            ).fetchone()["count"],
        }
    finally:
        conn.close()

    eval_home = tmp_path / "eval-agreement"
    assert run_cli(
        "eval",
        "--home",
        str(eval_home),
        "--fixture",
        str(SMALL),
        "--incremental-fixture",
        str(INCREMENTAL),
        "--json",
    ) == 0
    eval_metrics = json.loads(capsys.readouterr().out)["metrics"]

    assert daily_metrics["card_count"] == eval_metrics["card_count"] == 3
    assert daily_metrics["event_count"] == eval_metrics["event_count"] == 10
    assert daily_metrics["null_state_recurrence"] == eval_metrics["null_state_recurrence"] == 3
    assert daily_metrics["null_state_evidence"] == eval_metrics["null_state_evidence"] == 3
