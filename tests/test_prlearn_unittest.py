from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from prlearn.cli import main


ROOT = Path(__file__).resolve().parents[1]
SMALL = ROOT / "tests" / "fixtures" / "github_small.json"
INCREMENTAL = ROOT / "tests" / "fixtures" / "github_incremental.json"


class PrlearnCliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(list(args))
        self.assertEqual(code, 0)
        return buf.getvalue()

    def connect(self, home: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(home / "prlearn.db")
        conn.row_factory = sqlite3.Row
        return conn

    def test_daily_fixture_idempotent_incremental_dedupe_and_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.run_cli("daily", "--home", str(home), "--fixture", str(SMALL), "--json")
            conn = self.connect(home)
            first_cards = conn.execute("select count(*) as count from learning_cards").fetchone()["count"]
            first_events = conn.execute("select count(*) as count from events").fetchone()["count"]
            self.assertEqual(first_cards, 3)
            self.assertGreaterEqual(first_events, 7)
            conn.close()

            self.run_cli("daily", "--home", str(home), "--fixture", str(SMALL), "--json")
            conn = self.connect(home)
            self.assertEqual(conn.execute("select count(*) as count from learning_cards").fetchone()["count"], first_cards)
            self.assertEqual(conn.execute("select count(*) as count from events").fetchone()["count"], first_events)
            null_card = conn.execute("select * from learning_cards where mistake_pattern='tests-null-state'").fetchone()
            self.assertEqual(null_card["recurrence_count"], 2)
            conn.close()

            self.run_cli("daily", "--home", str(home), "--fixture", str(INCREMENTAL), "--json")
            conn = self.connect(home)
            self.assertEqual(conn.execute("select count(*) as count from learning_cards").fetchone()["count"], first_cards)
            null_card = conn.execute("select * from learning_cards where mistake_pattern='tests-null-state'").fetchone()
            self.assertEqual(null_card["recurrence_count"], 3)
            evidence = conn.execute("select count(*) as count from learning_evidence where learning_id=?", (null_card["id"],)).fetchone()["count"]
            self.assertEqual(evidence, 3)
            self.assertEqual(conn.execute("select count(*) as count from events where kind='pr_body'").fetchone()["count"], 1)
            self.assertEqual(conn.execute("select count(*) as count from events where kind='timeline_ready_for_review'").fetchone()["count"], 1)
            self.assertEqual(conn.execute("select count(*) as count from events where kind='check_annotation'").fetchone()["count"], 1)
            file_row = conn.execute("select raw_json from pr_files where path='src/dashboard.ts'").fetchone()
            self.assertIn("patch", file_row["raw_json"])
            self.assertTrue((home / "exports" / "LEARNINGS.md").exists())
            self.assertTrue((home / "exports" / "context.json").exists())
            self.assertTrue((home / "exports" / "rules.md").exists())
            conn.close()

    def test_preflight_export_and_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.run_cli("daily", "--home", str(home), "--fixture", str(SMALL))
            conn = self.connect(home)
            card_id = conn.execute("select id from learning_cards where mistake_pattern='webhook-signature'").fetchone()["id"]
            conn.close()
            self.run_cli("accept", str(card_id), "--home", str(home))
            out = self.run_cli("preflight", "--home", str(home), "--task", "build a Stripe webhook handler", "--top", "2")
            self.assertIn("Verify webhook signatures", out)
            out_dir = home / "out"
            self.run_cli("export", "--home", str(home), "--out", str(out_dir), "--format", "json")
            data = json.loads((out_dir / "context.json").read_text())
            self.assertTrue(data["learnings"])
            sched = self.run_cli("schedule", "print", "--home", str(home))
            self.assertIn("prlearn-daily", sched)

    def test_non_incremental_sync_twice_does_not_duplicate_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.run_cli("sync", "--home", str(home), "--fixture", str(SMALL))
            self.run_cli("sync", "--home", str(home), "--fixture", str(SMALL))
            conn = self.connect(home)
            try:
                self.assertEqual(conn.execute("select count(*) as count from prs").fetchone()["count"], 4)
                self.assertEqual(conn.execute("select count(*) as count from events").fetchone()["count"], 9)
                self.assertEqual(conn.execute("select count(*) as count from pr_files").fetchone()["count"], 4)
                self.assertEqual(conn.execute("select count(*) as count from check_runs").fetchone()["count"], 1)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
