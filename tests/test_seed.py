from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from prlearn.cli import main


ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "tests" / "fixtures" / "github_seed.json"


def run_cli(*args: str) -> int:
    return main(list(args))


def connect(home: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(home / "prlearn.db")
    conn.row_factory = sqlite3.Row
    return conn


def test_seed_fixture_backfills_prs_commits_and_is_idempotent(tmp_path: Path, capsys) -> None:
    assert run_cli(
        "seed",
        "--home",
        str(tmp_path),
        "--fixture",
        str(SEED),
        "--since",
        "2026-05-01T00:00:00Z",
        "--engine",
        "heuristic",
        "--json",
    ) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["repos_seen"] == 5
    assert stats["repos_selected"] == 2
    assert stats["repos_skipped"] == 3
    assert set(stats["selected_repos"]) == {"octo/app", "octo/payments"}
    assert stats["prs_seen"] == 2
    assert stats["prs_changed"] == 2
    assert stats["events_upserted"] == 2
    assert stats["commits_seen"] == 4
    assert stats["commit_signals"] == 3
    assert stats["commit_candidates_created"] == 3

    conn = connect(tmp_path)
    try:
        skipped = {
            row["repo_full_name"]: row["skipped_reason"]
            for row in conn.execute("select repo_full_name, skipped_reason from seed_repos").fetchall()
        }
        assert skipped["octo/old-tool"] == "inactive"
        assert skipped["octo/archived"] == "archived"
        assert skipped["octo/forked"] == "fork"
        assert skipped["octo/app"] is None
        assert skipped["octo/payments"] is None
        assert conn.execute("select count(*) as count from prs where number != 0").fetchone()["count"] == 2
        assert conn.execute("select count(*) as count from prs where number = 0").fetchone()["count"] == 2
        assert conn.execute("select count(*) as count from learning_cards").fetchone()["count"] == 2
        assert conn.execute("select count(*) as count from seed_commit_signals").fetchone()["count"] == 3
        assert conn.execute("select count(*) as count from learning_candidates where source='seed_commit'").fetchone()["count"] == 3
        assert conn.execute("select count(*) as count from learning_candidate_evidence").fetchone()["count"] >= 3
        cursor = conn.execute("select last_seeded_pr_at, last_seeded_commit_sha, last_seeded_commit_at from seed_repos where repo_full_name='octo/app'").fetchone()
        assert cursor["last_seeded_pr_at"] == "2026-05-20T12:00:00Z"
        assert cursor["last_seeded_commit_sha"] == "commit-copy"
        assert cursor["last_seeded_commit_at"] == "2026-05-21T12:00:00Z"
    finally:
        conn.close()

    assert run_cli(
        "seed",
        "--home",
        str(tmp_path),
        "--fixture",
        str(SEED),
        "--since",
        "2026-05-01T00:00:00Z",
        "--engine",
        "heuristic",
        "--json",
    ) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["prs_seen"] == 2
    assert second["prs_changed"] == 0
    assert second["events_upserted"] == 0
    assert second["commit_candidates_created"] == 0
    assert second["commit_candidates_updated"] == 3
    conn = connect(tmp_path)
    try:
        assert conn.execute("select count(*) as count from learning_cards").fetchone()["count"] == 2
        assert conn.execute("select count(*) as count from seed_commit_signals").fetchone()["count"] == 3
        assert conn.execute("select count(*) as count from learning_candidates where source='seed_commit'").fetchone()["count"] == 3
    finally:
        conn.close()


def test_seed_dry_run_does_not_write_inventory(tmp_path: Path, capsys) -> None:
    assert run_cli(
        "seed",
        "--home",
        str(tmp_path),
        "--fixture",
        str(SEED),
        "--since",
        "2026-05-01T00:00:00Z",
        "--dry-run",
        "--json",
    ) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["repos_seen"] == 5
    assert stats["repos_selected"] == 2
    assert stats["prs_seen"] == 2
    assert stats["commit_signals"] == 3
    conn = connect(tmp_path)
    try:
        assert conn.execute("select count(*) as count from seed_repos").fetchone()["count"] == 0
        assert conn.execute("select count(*) as count from prs").fetchone()["count"] == 0
        assert conn.execute("select count(*) as count from learning_candidates").fetchone()["count"] == 0
    finally:
        conn.close()
