from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

import pytest

from prlearn.cli import main
from prlearn.codex_client import client_from_config as codex_cli_client_from_config
from prlearn.db import MIGRATION_1, connect as db_connect, migrate
from prlearn.embeddings import blob_to_vector, store_embedding
from prlearn.evidence import ensure_evidence_for_dirty_prs
from prlearn.evaluate import compare_live_engines
from prlearn.learn import is_generic_candidate, limit_candidate_references, parse_model_json
from prlearn.ollama_client import OllamaClient, OllamaRemoteHostBlocked, OllamaUnavailable
from prlearn.openai_client import OpenAITimeout
from prlearn.prompts import SYSTEM_PROMPT, extraction_user_prompt
from prlearn.telegram_bot import TelegramError, apply_candidate_rating, discover_chats, parse_callback_data, poll_ratings, send_pending_candidates


ROOT = Path(__file__).resolve().parents[1]
SMALL = ROOT / "tests" / "fixtures" / "github_small.json"
INCREMENTAL = ROOT / "tests" / "fixtures" / "github_incremental.json"


def run_cli(*args: str) -> int:
    return main(list(args))


def connect(home: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(home / "prlearn.db")
    conn.row_factory = sqlite3.Row
    return conn


def parse_payload(messages: list[dict[str, str]]) -> dict[str, Any]:
    content = messages[-1]["content"]
    start = content.index('{"evidence_chunks"')
    return json.loads(content[start:])


def first_primary(payload: dict[str, Any]) -> dict[str, Any]:
    return next(chunk for chunk in payload["evidence_chunks"] if chunk["is_primary_signal"])


def test_ollama_localhost_guard_rejects_all_interface_bind() -> None:
    with pytest.raises(OllamaRemoteHostBlocked):
        OllamaClient(base_url="http://0.0.0.0:11434", require_localhost=True)
    assert OllamaClient(base_url="http://0.0.0.0:11434", require_localhost=False).base_url == "http://0.0.0.0:11434"


def null_state_response(payload: dict[str, Any]) -> dict[str, Any]:
    text = "\n".join(chunk["text"] for chunk in payload["evidence_chunks"]).lower()
    if "empty state" not in text and "null" not in text and "undefined" not in text:
        return {"candidates": []}
    chunk = first_primary(payload)
    return {
        "candidates": [
            {
                "title": "Cover null and empty states with regression tests",
                "lesson": "Review feedback showed null or empty states need explicit regression coverage.",
                "prevention_rule": "Before opening UI PRs, add regression tests for touched null, undefined, empty, and error states.",
                "mistake_pattern": "tests-null-state",
                "tags": ["tests", "edge-cases", "ui"],
                "severity": "medium",
                "confidence": "high",
                "learning_type": "test_gap",
                "evidence_ids": [chunk["evidence_id"]],
                "evidence_chunk_ids": [chunk["chunk_id"]],
                "evidence_quotes": [chunk["text"][:140]],
            }
        ]
    }


def duplicate_response(payload: dict[str, Any]) -> dict[str, Any]:
    response = null_state_response(payload)
    if not response["candidates"]:
        return response
    first = response["candidates"][0]
    second = {**first, "title": "Duplicate wording for the same evidence", "learning_type": "ci_failure"}
    return {"candidates": [first, second]}


def generic_response(payload: dict[str, Any]) -> dict[str, Any]:
    chunk = first_primary(payload)
    return {
        "candidates": [
            {
                "title": "Add tests",
                "lesson": "Add tests.",
                "prevention_rule": "Add tests.",
                "mistake_pattern": "generic-tests",
                "tags": ["tests"],
                "severity": "low",
                "confidence": 0.9,
                "learning_type": "test_gap",
                "evidence_ids": [chunk["evidence_id"]],
                "evidence_chunk_ids": [chunk["chunk_id"]],
                "evidence_quotes": [chunk["text"][:80]],
            }
        ]
    }


def nonexistent_evidence_response(payload: dict[str, Any]) -> dict[str, Any]:
    chunk = first_primary(payload)
    return {
        "candidates": [
            {
                "title": "Cover null and empty states with regression tests",
                "lesson": "Review feedback showed null or empty states need explicit regression coverage.",
                "prevention_rule": "Before opening UI PRs, add regression tests for touched null, undefined, empty, and error states.",
                "mistake_pattern": "tests-null-state",
                "tags": ["tests", "edge-cases", "ui"],
                "severity": "medium",
                "confidence": 0.86,
                "learning_type": "test_gap",
                "evidence_ids": ["ev_missing"],
                "evidence_chunk_ids": [chunk["chunk_id"]],
                "evidence_quotes": [chunk["text"][:80]],
            }
        ]
    }


def paraphrased_quote_response(payload: dict[str, Any]) -> dict[str, Any]:
    chunk = first_primary(payload)
    return {
        "candidates": [
            {
                "title": "Cover null and empty states with regression tests",
                "lesson": "Review feedback showed null or empty states need explicit regression coverage.",
                "prevention_rule": "Before opening UI PRs, add regression tests for touched null, undefined, empty, and error states.",
                "mistake_pattern": "tests-null-state",
                "tags": ["tests", "edge-cases", "ui"],
                "severity": "medium",
                "confidence": 0.86,
                "learning_type": "test_gap",
                "evidence_ids": [chunk["evidence_id"]],
                "evidence_chunk_ids": [chunk["chunk_id"]],
                "evidence_quotes": ["The reviewer wanted more coverage for empty states."],
            }
        ]
    }


class FakeOllama:
    def __init__(self, factory: Callable[[dict[str, Any]], dict[str, Any]] | None = None, *, invalid_json: bool = False) -> None:
        self.factory = factory or null_state_response
        self.invalid_json = invalid_json
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []

    @property
    def model_label(self) -> str:
        return "fake-codex"

    def version(self) -> dict[str, str]:
        return {"version": "test"}

    def installed_models(self) -> set[str]:
        return {"qwen3.5:9b", "qwen3.5:2b", "qwen3-embedding:0.6b"}

    def chat(self, *, model: str, messages: list[dict[str, str]], options: dict[str, Any], schema: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls += 1
        self.messages.append(messages)
        if self.invalid_json:
            return {"message": {"content": "not json"}}
        payload = parse_payload(messages)
        response = self.factory(payload)
        content = response if isinstance(response, str) else json.dumps(response)
        return {"message": {"content": content}}


def patch_ollama(monkeypatch: pytest.MonkeyPatch, fake: FakeOllama) -> FakeOllama:
    monkeypatch.setattr("prlearn.learn.client_from_config", lambda *args, **kwargs: fake)
    return fake


def patch_codex(monkeypatch: pytest.MonkeyPatch, fake: FakeOllama) -> FakeOllama:
    monkeypatch.setattr("prlearn.learn.codex_client_from_config", lambda *args, **kwargs: fake)
    return fake


class FakeOpenAI(FakeOllama):
    @property
    def model_label(self) -> str:
        return "gpt-5.5-test"


class FailingAfterFirstOpenAI(FakeOpenAI):
    def chat(self, *, model: str, messages: list[dict[str, str]], options: dict[str, Any], schema: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.calls >= 1:
            self.calls += 1
            raise OpenAITimeout("simulated timeout")
        return super().chat(model=model, messages=messages, options=options, schema=schema)


def patch_openai(monkeypatch: pytest.MonkeyPatch, fake: FakeOpenAI) -> FakeOpenAI:
    monkeypatch.setattr("prlearn.learn.openai_client_from_config", lambda *args, **kwargs: fake)
    return fake


class FakeTelegram:
    def __init__(self, updates: list[dict[str, Any]] | None = None, *, fail_answers: bool = False) -> None:
        self.messages: list[dict[str, Any]] = []
        self.answered: list[dict[str, str]] = []
        self.deleted: list[dict[str, Any]] = []
        self.updates = updates or []
        self.fail_answers = fail_answers

    def send_message(self, *, chat_id: str, text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        self.messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True, "result": {"message_id": len(self.messages)}}

    def get_updates(self, *, offset: int | None, timeout: int, limit: int, allowed_updates: list[str] | None = None) -> list[dict[str, Any]]:
        return self.updates[:limit]

    def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        if self.fail_answers:
            raise TelegramError("answer failed")
        self.answered.append({"id": callback_query_id, "text": text})

    def delete_message(self, *, chat_id: str, message_id: int) -> None:
        self.deleted.append({"chat_id": chat_id, "message_id": message_id})


def test_v2_migration_is_idempotent_for_fresh_and_existing_v1_db(tmp_path: Path) -> None:
    assert run_cli("init", "--home", str(tmp_path)) == 0
    conn = connect(tmp_path)
    try:
        versions = [row["version"] for row in conn.execute("select version from schema_migrations order by version")]
        assert versions == [1, 2, 3, 4, 5]
        tables = {row["name"] for row in conn.execute("select name from sqlite_master where type='table'")}
        for table in [
            "evidence_items",
            "evidence_chunks",
            "model_runs",
            "learning_candidates",
            "learning_candidate_evidence",
            "learning_card_recurrences",
            "learning_card_feedback",
            "embeddings",
            "learning_candidate_ratings",
            "telegram_candidate_messages",
            "seed_repos",
            "seed_commit_signals",
        ]:
            assert table in tables
        migrate(conn)
        assert [row["version"] for row in conn.execute("select version from schema_migrations order by version")] == [1, 2, 3, 4, 5]
        embedding_id = store_embedding(conn, owner_type="chunk", owner_id="chunk-1", model_name="test-embed", text="hello", vector=[0.1, 0.2])
        row = conn.execute("select * from embeddings where id=?", (embedding_id,)).fetchone()
        assert row["dimensions"] == 2
        assert blob_to_vector(row["vector_blob"]) == pytest.approx([0.1, 0.2])
    finally:
        conn.close()

    legacy = tmp_path / "legacy.db"
    conn = db_connect(legacy)
    try:
        conn.executescript(MIGRATION_1)
        conn.execute("insert or ignore into schema_migrations(version, applied_at) values(1, '2026-01-01T00:00:00Z')")
        conn.execute(
            """
            insert into learning_cards(canonical_key, title, lesson, prevention_rule, tags_json, contexts_json,
              severity, confidence, status, recurrence_count, first_seen_at, last_seen_at, created_at, updated_at)
            values('k', 't', 'l', 'p', '[]', '[]', 1, 0.5, 'pending', 1, 'now', 'now', 'now', 'now')
            """
        )
        conn.commit()
        migrate(conn)
        migrate(conn)
        assert conn.execute("select count(*) as count from learning_cards").fetchone()["count"] == 1
        assert conn.execute("select count(*) as count from schema_migrations where version=2").fetchone()["count"] == 1
        assert conn.execute("select count(*) as count from schema_migrations where version=3").fetchone()["count"] == 1
        assert conn.execute("select count(*) as count from schema_migrations where version=4").fetchone()["count"] == 1
        assert conn.execute("select count(*) as count from schema_migrations where version=5").fetchone()["count"] == 1
    finally:
        conn.close()


def test_config_defaults_and_doctor_ollama_warning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert run_cli("init", "--home", str(tmp_path)) == 0
    config = json.loads((tmp_path / "config.json").read_text())
    assert config["ollama"]["base_url"] == "http://127.0.0.1:11434"
    assert config["ollama"]["request_timeout_seconds"] == 180
    assert config["ollama"]["options"]["temperature"] == 0
    assert config["ollama"]["options"]["num_predict"] == 2048
    assert config["codex"]["command"] == "codex"
    assert config["codex"]["model"] == "gpt-5.5"
    assert config["codex"]["reasoning_effort"] == "low"
    assert config["codex"]["include_raw_metadata"] is True
    assert config["openai"]["api_key_env"] == "OPENAI_API_KEY"
    assert config["openai"]["model"] == "gpt-5.5"
    assert config["openai"]["reasoning_effort"] == "low"
    assert config["openai"]["max_output_tokens"] == 2048
    assert config["openai"]["include_raw_metadata"] is True

    monkeypatch.setattr("prlearn.doctor.client_from_config", lambda *args, **kwargs: (_ for _ in ()).throw(OllamaUnavailable("offline")))
    assert run_cli("doctor", "--home", str(tmp_path), "--json") == 0
    assert run_cli("doctor", "--home", str(tmp_path), "--strict-ollama", "--json") == 1


def test_ollama_prompt_names_required_candidate_fields() -> None:
    prompt = SYSTEM_PROMPT + "\n" + extraction_user_prompt('{"evidence_chunks":[]}')
    for key in ["prevention_rule", "mistake_pattern", "evidence_ids", "evidence_chunk_ids"]:
        assert key in prompt
    assert "at most 1" in prompt
    assert "Do not use these keys: trigger_conditions, prevention_action" in prompt


def test_codex_engine_uses_codex_source_and_raw_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = patch_codex(monkeypatch, FakeOllama())
    assert run_cli("sync", "--home", str(tmp_path), "--fixture", str(SMALL)) == 0
    assert run_cli("extract", "--home", str(tmp_path), "--engine", "codex", "--json") == 0
    conn = connect(tmp_path)
    try:
        candidate = conn.execute("select * from learning_candidates where source='codex'").fetchone()
        assert candidate is not None
        run = conn.execute("select * from model_runs where model_name='fake-codex'").fetchone()
        assert run is not None
        assert run["run_type"] == "codex_candidate_extraction"
        assert "octo/app" in run["request_json"]
        assert "[REDACTED_REPO_" not in run["request_json"]
    finally:
        conn.close()
    assert fake.calls >= 1
    assert "Review these evidence chunks" in fake.messages[0][-1]["content"]


def test_openai_engine_uses_openai_source_raw_metadata_and_model_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = patch_openai(monkeypatch, FakeOpenAI())
    assert run_cli("sync", "--home", str(tmp_path), "--fixture", str(SMALL)) == 0
    assert run_cli("extract", "--home", str(tmp_path), "--engine", "openai", "--max-model-prs", "1", "--json") == 0
    conn = connect(tmp_path)
    try:
        candidate = conn.execute("select * from learning_candidates where source='openai'").fetchone()
        assert candidate is not None
        run = conn.execute("select * from model_runs where run_type='openai_candidate_extraction'").fetchone()
        assert run is not None
        assert run["model_name"] == "gpt-5.5-test"
        options = json.loads(run["options_json"])
        assert options["model"] == "gpt-5.5"
        assert options["reasoning_effort"] == "low"
        assert options["max_output_tokens"] == 2048
        assert "octo/app" in run["request_json"]
        assert "[REDACTED_REPO_" not in run["request_json"]
    finally:
        conn.close()
    assert fake.calls == 1
    assert "Review these evidence chunks" in fake.messages[0][-1]["content"]


def test_openai_extract_commits_each_pr_before_later_provider_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = patch_openai(monkeypatch, FailingAfterFirstOpenAI())
    assert run_cli("sync", "--home", str(tmp_path), "--fixture", str(SMALL)) == 0
    assert run_cli("extract", "--home", str(tmp_path), "--engine", "openai", "--json") == 1
    conn = connect(tmp_path)
    try:
        assert conn.execute("select count(*) as count from learning_candidates where source='openai'").fetchone()["count"] == 1
        assert conn.execute("select count(*) as count from model_runs where run_type='openai_candidate_extraction' and status='success'").fetchone()["count"] == 1
        assert conn.execute("select count(*) as count from model_runs where run_type='openai_candidate_extraction' and status='failed'").fetchone()["count"] == 1
    finally:
        conn.close()
    assert fake.calls == 2


def test_compare_live_engines_isolates_each_provider_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def fake_github_client(config: dict[str, Any], mode: str | None) -> object:
        assert mode == "app"
        return object()

    def fake_seed(conn: sqlite3.Connection, client: object, options: dict[str, Any]) -> dict[str, Any]:
        del conn, client
        calls.append({"engine": options["engine"], "db": options["config"]["db"], "max_model_prs": options["max_model_prs"]})
        return {
            "repos_seen": 1,
            "repos_selected": 1,
            "selected_repos": ["octo/app"],
            "prs_seen": 2,
            "commits_seen": 0,
        }

    monkeypatch.setattr("prlearn.evaluate.github_client_for", fake_github_client)
    monkeypatch.setattr("prlearn.evaluate.seed_account", fake_seed)
    result = compare_live_engines(
        base_home=tmp_path / "base",
        base_db_path=tmp_path / "base" / "prlearn.db",
        engines=["heuristic", "openai"],
        seed_options={"max_model_prs": 3},
        github_auth="app",
    )
    assert result["passed"] is True
    assert [call["engine"] for call in calls] == ["heuristic", "openai"]
    assert {call["max_model_prs"] for call in calls} == {3}
    assert calls[0]["db"] != calls[1]["db"]


def test_codex_client_config_defaults_to_gpt55_low_reasoning() -> None:
    client = codex_cli_client_from_config({"command": "codex", "model": "gpt-5.5", "reasoning_effort": "low"})
    assert client.model == "gpt-5.5"
    assert client.reasoning_effort == "low"


def test_specific_candidate_is_not_rejected_when_trigger_is_in_lesson() -> None:
    assert is_generic_candidate({"title": "Add tests", "lesson": "Add tests.", "prevention_rule": "Add tests."})
    assert not is_generic_candidate(
        {
            "title": "Handle undefined/null UI states in profile rendering",
            "lesson": "Always validate that profile data exists before rendering UI components when the profile is undefined or null.",
            "prevention_rule": "Implement defensive checks and add regression tests for these empty states.",
        }
    )


def test_parse_model_json_repairs_missing_suffix() -> None:
    data = parse_model_json('{"candidates":[{"title":"Webhook","evidence_ids":["ev_1"]}')
    assert data["candidates"][0]["title"] == "Webhook"


def test_candidate_reference_lists_are_deduped_and_bounded() -> None:
    candidate = limit_candidate_references(
        {
            "evidence_ids": ["ev_1", "ev_1", "ev_2", "ev_3", "ev_4", "ev_5"],
            "evidence_chunk_ids": ["chk_1", "chk_2", "chk_3", "chk_4", "chk_5"],
            "evidence_quotes": ["a", "a", "b", "c", "d", "e"],
        }
    )
    assert candidate["evidence_ids"] == ["ev_1", "ev_2", "ev_3", "ev_4"]
    assert candidate["evidence_chunk_ids"] == ["chk_1", "chk_2", "chk_3", "chk_4"]
    assert candidate["evidence_quotes"] == ["a", "b", "c", "d"]


def test_hybrid_falls_back_and_ollama_strict_fails_without_corrupting_cards(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("prlearn.learn.client_from_config", lambda *args, **kwargs: (_ for _ in ()).throw(OllamaUnavailable("offline")))
    assert run_cli("daily", "--home", str(tmp_path), "--fixture", str(SMALL), "--engine", "hybrid", "--json") == 0
    conn = connect(tmp_path)
    try:
        assert conn.execute("select count(*) as count from learning_cards").fetchone()["count"] == 3
    finally:
        conn.close()

    strict_home = tmp_path / "strict"
    assert run_cli("daily", "--home", str(strict_home), "--fixture", str(SMALL), "--engine", "ollama", "--json") == 1
    conn = connect(strict_home)
    try:
        assert conn.execute("select count(*) as count from learning_cards").fetchone()["count"] == 0
        assert conn.execute("select count(*) as count from learning_candidates").fetchone()["count"] == 0
    finally:
        conn.close()


def test_ollama_context_only_mode_is_explicit_opt_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fixture = tmp_path / "context-only.json"
    fixture.write_text(
        json.dumps(
            {
                "prs": [
                    {
                        "repo_full_name": "octo/context",
                        "number": 10,
                        "github_id": "PR_context",
                        "title": "Refactor dashboard data loading",
                        "body": "Refactor dashboard data loading to use a shared loader and simplify error handling.",
                        "url": "https://github.com/octo/context/pull/10",
                        "author_login": "coder",
                        "state": "OPEN",
                        "created_at": "2026-05-01T10:00:00Z",
                        "updated_at": "2026-05-01T11:00:00Z",
                    }
                ]
            }
        )
    )

    def context_response(payload: dict[str, Any]) -> dict[str, Any]:
        chunk = payload["evidence_chunks"][0]
        return {
            "candidates": [
                {
                    "title": "Centralize dashboard data loading before branching UI states",
                    "lesson": "The PR context shows dashboard loading logic being refactored into a shared path.",
                    "prevention_rule": "When refactoring dashboard data loading, centralize the loader before adding branch-specific UI states.",
                    "mistake_pattern": "dashboard-loader-branching",
                    "tags": ["frontend", "data-loading", "maintainability"],
                    "severity": "medium",
                    "confidence": 0.72,
                    "learning_type": "maintainability",
                    "evidence_ids": [chunk["evidence_id"]],
                    "evidence_chunk_ids": [chunk["chunk_id"]],
                    "evidence_quotes": [],
                }
            ]
        }

    skipped_fake = patch_ollama(monkeypatch, FakeOllama(context_response))
    assert run_cli("sync", "--home", str(tmp_path), "--fixture", str(fixture)) == 0
    assert run_cli("extract", "--home", str(tmp_path), "--engine", "ollama", "--json") == 0
    assert skipped_fake.calls == 0
    conn = connect(tmp_path)
    try:
        assert conn.execute("select count(*) as count from learning_candidates").fetchone()["count"] == 0
    finally:
        conn.close()

    opt_in_home = tmp_path / "opt-in"
    opt_in_fake = patch_ollama(monkeypatch, FakeOllama(context_response))
    assert run_cli("sync", "--home", str(opt_in_home), "--fixture", str(fixture)) == 0
    assert run_cli("extract", "--home", str(opt_in_home), "--engine", "ollama", "--allow-context-only", "--json") == 0
    assert opt_in_fake.calls == 1
    conn = connect(opt_in_home)
    try:
        candidate = conn.execute("select * from learning_candidates where status='pending'").fetchone()
        assert candidate is not None
        assert candidate["confidence"] == pytest.approx(0.72)
    finally:
        conn.close()


def test_evidence_selection_marks_review_feedback_typecheck_self_status_and_bot_noise(tmp_path: Path) -> None:
    assert run_cli("sync", "--home", str(tmp_path), "--fixture", str(SMALL)) == 0
    conn = connect(tmp_path)
    try:
        stats = ensure_evidence_for_dirty_prs(conn)
        assert stats["evidence_items_created"] > 0
        primary_kinds = {
            row["source_kind"]
            for row in conn.execute("select source_kind from evidence_items where is_primary_signal=1")
        }
        assert "review_body" in primary_kinds
        assert "review_comment" in primary_kinds
        assert "check_annotation" in primary_kinds
    finally:
        conn.close()

    fixture = tmp_path / "noise.json"
    fixture.write_text(
        json.dumps(
            {
                "prs": [
                    {
                        "repo_full_name": "octo/noise",
                        "number": 1,
                        "github_id": "PR_noise",
                        "title": "Status only",
                        "url": "https://github.com/octo/noise/pull/1",
                        "author_login": "coder",
                        "state": "OPEN",
                        "created_at": "2026-05-01T10:00:00Z",
                        "updated_at": "2026-05-01T11:00:00Z",
                        "issue_comments": [
                            {"id": "self-1", "author": {"login": "coder"}, "body": "Fixed, thanks.", "created_at": "2026-05-01T10:30:00Z"},
                            {"id": "bot-1", "author": {"login": "github-actions[bot]"}, "body": "CI passed, deployed preview.", "created_at": "2026-05-01T10:35:00Z"},
                        ],
                    }
                ]
            }
        )
    )
    noise_home = tmp_path / "noise-home"
    assert run_cli("sync", "--home", str(noise_home), "--fixture", str(fixture)) == 0
    conn = connect(noise_home)
    try:
        ensure_evidence_for_dirty_prs(conn)
        assert conn.execute("select count(*) as count from evidence_items where is_primary_signal=1").fetchone()["count"] == 0
    finally:
        conn.close()

    polite_fixture = tmp_path / "polite-review.json"
    polite_fixture.write_text(
        json.dumps(
            {
                "prs": [
                    {
                        "repo_full_name": "octo/polite",
                        "number": 2,
                        "github_id": "PR_polite",
                        "title": "Handle empty state",
                        "url": "https://github.com/octo/polite/pull/2",
                        "author_login": "coder",
                        "state": "OPEN",
                        "created_at": "2026-05-01T10:00:00Z",
                        "updated_at": "2026-05-01T11:00:00Z",
                        "review_comments": [
                            {
                                "id": "review-comment-1",
                                "author": {"login": "reviewer"},
                                "body": "Thanks, but please add a regression test for the empty state.",
                                "path": "src/view.tsx",
                                "line": 42,
                                "created_at": "2026-05-01T10:35:00Z",
                            }
                        ],
                    }
                ]
            }
        )
    )
    polite_home = tmp_path / "polite-home"
    assert run_cli("sync", "--home", str(polite_home), "--fixture", str(polite_fixture)) == 0
    conn = connect(polite_home)
    try:
        ensure_evidence_for_dirty_prs(conn)
        row = conn.execute("select is_primary_signal, is_noise from evidence_items where source_kind='review_comment'").fetchone()
        assert row["is_primary_signal"] == 1
        assert row["is_noise"] == 0
    finally:
        conn.close()

    safety_fixture = tmp_path / "safety-context.json"
    safety_fixture.write_text(
        json.dumps(
            {
                "prs": [
                    {
                        "repo_full_name": "octo/safety",
                        "number": 3,
                        "github_id": "PR_safety",
                        "title": "Add staging fixture writer",
                        "url": "https://github.com/octo/safety/pull/3",
                        "author_login": "coder",
                        "state": "OPEN",
                        "body": (
                            "Safety boundary check: no live venue order placement. "
                            "Fixture writes require STAGING_TARGET=staging and the script refuses production-looking targets."
                        ),
                        "created_at": "2026-05-01T10:00:00Z",
                        "updated_at": "2026-05-01T11:00:00Z",
                    },
                    {
                        "repo_full_name": "octo/safety",
                        "number": 4,
                        "github_id": "PR_failed_check",
                        "title": "Fix test output",
                        "url": "https://github.com/octo/safety/pull/4",
                        "author_login": "coder",
                        "state": "OPEN",
                        "created_at": "2026-05-01T10:00:00Z",
                        "updated_at": "2026-05-01T11:00:00Z",
                        "check_runs": [{"id": "check-1", "name": "test", "conclusion": "failure", "output": {}}],
                    },
                ]
            }
        )
    )
    safety_home = tmp_path / "safety-home"
    assert run_cli("sync", "--home", str(safety_home), "--fixture", str(safety_fixture)) == 0
    conn = connect(safety_home)
    try:
        ensure_evidence_for_dirty_prs(conn)
        primary_kinds = {
            row["source_kind"]
            for row in conn.execute("select source_kind from evidence_items where is_primary_signal=1")
        }
        assert "pr_body" in primary_kinds
        assert "failed_check" in primary_kinds
        assert conn.execute("select count(*) as count from evidence_items where source_kind='pr_context'").fetchone()["count"] == 0
    finally:
        conn.close()


def test_mock_ollama_creates_reviewable_candidate_cache_hit_and_accepts_recurrence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = patch_ollama(monkeypatch, FakeOllama())
    assert run_cli("sync", "--home", str(tmp_path), "--fixture", str(SMALL)) == 0
    assert run_cli("extract", "--home", str(tmp_path), "--engine", "ollama", "--json") == 0
    conn = connect(tmp_path)
    try:
        assert conn.execute("select count(*) as count from learning_cards").fetchone()["count"] == 0
        candidate = conn.execute("select * from learning_candidates where status='pending'").fetchone()
        assert candidate is not None
        assert conn.execute("select count(*) as count from model_runs where status='success'").fetchone()["count"] >= 1
    finally:
        conn.close()
    first_calls = fake.calls
    assert run_cli("extract", "--home", str(tmp_path), "--engine", "ollama", "--all", "--json") == 0
    assert fake.calls == first_calls
    conn = connect(tmp_path)
    try:
        assert conn.execute("select count(*) as count from model_runs where status='success' and attempts=1").fetchone()["count"] >= 1
    finally:
        conn.close()

    assert run_cli("accept-candidate", candidate["id"], "--home", str(tmp_path)) == 0
    conn = connect(tmp_path)
    try:
        card_count = conn.execute("select count(*) as count from learning_cards").fetchone()["count"]
        assert card_count == 1
    finally:
        conn.close()

    assert run_cli("sync", "--home", str(tmp_path), "--fixture", str(INCREMENTAL)) == 0
    assert run_cli("extract", "--home", str(tmp_path), "--engine", "ollama", "--json") == 0
    conn = connect(tmp_path)
    try:
        assert conn.execute("select count(*) as count from learning_cards").fetchone()["count"] == 1
        card = conn.execute("select * from learning_cards").fetchone()
        assert card["recurrence_count"] >= 2
        assert conn.execute("select count(*) as count from learning_card_recurrences where learning_id=?", (card["id"],)).fetchone()["count"] >= 2
    finally:
        conn.close()


def test_candidate_reject_and_merge_commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    reject_home = tmp_path / "reject-candidate"
    patch_ollama(monkeypatch, FakeOllama())
    assert run_cli("sync", "--home", str(reject_home), "--fixture", str(SMALL)) == 0
    assert run_cli("extract", "--home", str(reject_home), "--engine", "ollama", "--json") == 0
    assert run_cli("review", "--home", str(reject_home), "--candidates", "--json") == 0
    conn = connect(reject_home)
    try:
        candidate = conn.execute("select * from learning_candidates where status='pending'").fetchone()
    finally:
        conn.close()
    assert candidate is not None
    assert run_cli("reject-candidate", candidate["id"], "--home", str(reject_home), "--reason", "too broad") == 0
    assert run_cli("extract", "--home", str(reject_home), "--engine", "ollama", "--all", "--json") == 0
    conn = connect(reject_home)
    try:
        assert conn.execute("select count(*) as count from learning_candidates where status='rejected'").fetchone()["count"] == 1
        assert conn.execute("select count(*) as count from learning_candidates where status='pending'").fetchone()["count"] == 0
    finally:
        conn.close()

    merge_home = tmp_path / "merge-candidate"
    patch_ollama(monkeypatch, FakeOllama())
    assert run_cli("daily", "--home", str(merge_home), "--fixture", str(SMALL), "--json") == 0
    assert run_cli("extract", "--home", str(merge_home), "--engine", "ollama", "--all", "--json") == 0
    conn = connect(merge_home)
    try:
        card = conn.execute("select * from learning_cards where mistake_pattern='tests-null-state'").fetchone()
        candidate = conn.execute("select * from learning_candidates where status='pending'").fetchone()
    finally:
        conn.close()
    assert card is not None
    assert candidate is not None
    assert run_cli("merge-candidate", candidate["id"], str(card["id"]), "--home", str(merge_home)) == 0
    conn = connect(merge_home)
    try:
        assert conn.execute("select status from learning_candidates where id=?", (candidate["id"],)).fetchone()["status"] == "merged"
        assert conn.execute("select count(*) as count from learning_card_recurrences where learning_id=?", (card["id"],)).fetchone()["count"] >= 1
    finally:
        conn.close()


def test_telegram_candidate_send_and_rating_actions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_ollama(monkeypatch, FakeOllama())
    assert run_cli("sync", "--home", str(tmp_path), "--fixture", str(SMALL)) == 0
    assert run_cli("extract", "--home", str(tmp_path), "--engine", "ollama", "--json") == 0
    conn = connect(tmp_path)
    try:
        candidate = conn.execute("select * from learning_candidates where status='pending'").fetchone()
        assert candidate is not None
        fake = FakeTelegram()
        send_stats = send_pending_candidates(conn, fake, chat_id="123", limit=5)
        assert send_stats["sent"] == 1
        assert fake.messages[0]["chat_id"] == "123"
        assert "Major learning" in json.dumps(fake.messages[0]["reply_markup"])
        assert str(candidate["id"]) in fake.messages[0]["text"]
        major_callback = fake.messages[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        assert parse_callback_data(major_callback)[:2] == ("major", candidate["id"])
        assert parse_callback_data(f"prlearn:major:{candidate['id']}") is None
        assert conn.execute("select count(*) as count from telegram_candidate_messages where candidate_id=?", (candidate["id"],)).fetchone()["count"] == 1
        chat_fake = FakeTelegram(
            [
                {"update_id": 1, "message": {"chat": {"id": 123, "type": "private", "first_name": "Reviewer"}}},
                {
                    "update_id": 2,
                    "callback_query": {
                        "id": "cb-chat",
                        "message": {"chat": {"id": 123, "type": "private", "first_name": "Reviewer"}, "message_id": 1},
                        "data": fake.messages[0]["reply_markup"]["inline_keyboard"][0][1]["callback_data"],
                    },
                },
            ]
        )
        assert discover_chats(chat_fake) == [{"id": "123", "type": "private", "title": "Reviewer"}]

        result = apply_candidate_rating(conn, candidate_id=candidate["id"], rating="major", telegram_user_id="42", telegram_chat_id="123")
        assert result["ok"] is True
        assert result["action"] == "accepted"
        assert conn.execute("select status from learning_candidates where id=?", (candidate["id"],)).fetchone()["status"] == "accepted"
        card = conn.execute("select * from learning_cards where id=?", (result["learning_id"],)).fetchone()
        assert card["status"] == "accepted"
        assert card["severity"] == 5
        rating = conn.execute("select * from learning_candidate_ratings where candidate_id=?", (candidate["id"],)).fetchone()
        assert rating["rating"] == "major"
        assert rating["telegram_chat_id_hash"] != "123"
        replay = apply_candidate_rating(conn, candidate_id=candidate["id"], rating="minor", telegram_user_id="42", telegram_chat_id="123")
        assert replay["ok"] is False
        assert replay["action"] == "already_accepted"
        assert conn.execute("select severity from learning_cards where id=?", (result["learning_id"],)).fetchone()["severity"] == 5
    finally:
        conn.close()

    monkeypatch.delenv("PRLEARN_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("PRLEARN_TELEGRAM_CHAT_ID", raising=False)
    assert run_cli("telegram", "send", "--home", str(tmp_path / "dry-run"), "--dry-run", "--json") == 0

    reject_home = tmp_path / "reject"
    patch_ollama(monkeypatch, FakeOllama())
    assert run_cli("sync", "--home", str(reject_home), "--fixture", str(SMALL)) == 0
    assert run_cli("extract", "--home", str(reject_home), "--engine", "ollama", "--json") == 0
    conn = connect(reject_home)
    try:
        candidate = conn.execute("select * from learning_candidates where status='pending'").fetchone()
        assert candidate is not None
        sender = FakeTelegram()
        send_pending_candidates(conn, sender, chat_id="123", limit=5)
        rating_data = sender.messages[0]["reply_markup"]["inline_keyboard"][1][0]["callback_data"]
        missing_chat = FakeTelegram(
            [
                {
                    "update_id": 8,
                    "callback_query": {
                        "id": "cb-missing-chat",
                        "from": {"id": 42},
                        "data": rating_data,
                    },
                }
            ]
        )
        stats = poll_ratings(conn, missing_chat, chat_id="123")
        assert stats["processed"] == 0
        assert stats["ignored"] == 1
        assert missing_chat.answered[0]["text"] == "This prlearn bot is locked to another chat."
        assert conn.execute("select status from learning_candidates where id=?", (candidate["id"],)).fetchone()["status"] == "pending"

        forged = FakeTelegram(
            [
                {
                    "update_id": 9,
                    "callback_query": {
                        "id": "cb-forged",
                        "from": {"id": 42},
                        "message": {"chat": {"id": 123}, "message_id": 999},
                        "data": f"prlearn:major:{candidate['id']}:forged",
                    },
                }
            ]
        )
        stats = poll_ratings(conn, forged, chat_id="123")
        assert stats["processed"] == 0
        assert stats["ignored"] == 1
        assert forged.answered[0]["text"] == "This review button is no longer valid."
        assert conn.execute("select status from learning_candidates where id=?", (candidate["id"],)).fetchone()["status"] == "pending"

        fake = FakeTelegram(
            [
                {
                    "update_id": 10,
                    "callback_query": {
                        "id": "cb-1",
                        "from": {"id": 42},
                        "message": {"chat": {"id": 123}, "message_id": 1},
                        "data": rating_data,
                    },
                }
            ]
        )
        stats = poll_ratings(conn, fake, chat_id="123")
        assert stats["processed"] == 1
        assert stats["deleted"] == 1
        assert stats["delete_failed"] == 0
        assert fake.answered[0]["text"] == "Recorded: not important"
        assert fake.deleted == [{"chat_id": "123", "message_id": 1}]
        assert conn.execute("select status from learning_candidates where id=?", (candidate["id"],)).fetchone()["status"] == "rejected"
        assert conn.execute("select reviewed_at from telegram_candidate_messages where candidate_id=?", (candidate["id"],)).fetchone()["reviewed_at"] is not None
        assert conn.execute("select value from sync_state where key='telegram_last_update_id'").fetchone()["value"] == "10"
    finally:
        conn.close()

    ack_home = tmp_path / "ack-failure"
    patch_ollama(monkeypatch, FakeOllama())
    assert run_cli("sync", "--home", str(ack_home), "--fixture", str(SMALL)) == 0
    assert run_cli("extract", "--home", str(ack_home), "--engine", "ollama", "--json") == 0
    conn = connect(ack_home)
    try:
        candidate = conn.execute("select * from learning_candidates where status='pending'").fetchone()
        assert candidate is not None
        sender = FakeTelegram()
        send_pending_candidates(conn, sender, chat_id="123", limit=5)
        rating_data = sender.messages[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        failing_answer = FakeTelegram(
            [
                {
                    "update_id": 11,
                    "callback_query": {
                        "id": "cb-too-old",
                        "from": {"id": 42},
                        "message": {"chat": {"id": 123}, "message_id": 1},
                        "data": rating_data,
                    },
                }
            ],
            fail_answers=True,
        )
        stats = poll_ratings(conn, failing_answer, chat_id="123")
        assert stats["processed"] == 1
        assert stats["answer_failed"] == 1
        assert stats["deleted"] == 1
        assert conn.execute("select status from learning_candidates where id=?", (candidate["id"],)).fetchone()["status"] == "accepted"
        assert conn.execute("select reviewed_at from telegram_candidate_messages where candidate_id=?", (candidate["id"],)).fetchone()["reviewed_at"] is not None
        assert conn.execute("select value from sync_state where key='telegram_last_update_id'").fetchone()["value"] == "11"
    finally:
        conn.close()


def test_duplicate_ollama_candidates_with_same_evidence_are_collapsed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_ollama(monkeypatch, FakeOllama(duplicate_response))
    assert run_cli("sync", "--home", str(tmp_path), "--fixture", str(SMALL)) == 0
    assert run_cli("extract", "--home", str(tmp_path), "--engine", "ollama", "--json") == 0
    conn = connect(tmp_path)
    try:
        assert conn.execute("select count(*) as count from learning_candidates").fetchone()["count"] == 1
    finally:
        conn.close()


def test_generic_candidate_invalid_json_and_secret_redaction(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = patch_ollama(monkeypatch, FakeOllama(generic_response))
    assert run_cli("sync", "--home", str(tmp_path), "--fixture", str(SMALL)) == 0
    assert run_cli("extract", "--home", str(tmp_path), "--engine", "ollama", "--json") == 0
    conn = connect(tmp_path)
    try:
        assert fake.calls >= 1
        assert conn.execute("select count(*) as count from learning_candidates").fetchone()["count"] == 0
    finally:
        conn.close()

    missing_home = tmp_path / "missing-evidence"
    patch_ollama(monkeypatch, FakeOllama(nonexistent_evidence_response))
    assert run_cli("sync", "--home", str(missing_home), "--fixture", str(SMALL)) == 0
    assert run_cli("extract", "--home", str(missing_home), "--engine", "ollama", "--json") == 0
    conn = connect(missing_home)
    try:
        assert conn.execute("select count(*) as count from learning_candidates").fetchone()["count"] == 0
    finally:
        conn.close()

    paraphrase_home = tmp_path / "paraphrase"
    patch_ollama(monkeypatch, FakeOllama(paraphrased_quote_response))
    assert run_cli("sync", "--home", str(paraphrase_home), "--fixture", str(SMALL)) == 0
    assert run_cli("extract", "--home", str(paraphrase_home), "--engine", "ollama", "--json") == 0
    conn = connect(paraphrase_home)
    try:
        assert conn.execute("select count(*) as count from learning_candidates").fetchone()["count"] == 1
        evidence = conn.execute("select quote from learning_candidate_evidence").fetchone()
        assert evidence is not None
        assert evidence["quote"] is None
    finally:
        conn.close()

    fenced_home = tmp_path / "fenced-json"
    patch_ollama(monkeypatch, FakeOllama(lambda payload: "```json\n" + json.dumps(null_state_response(payload)) + "\n```"))
    assert run_cli("sync", "--home", str(fenced_home), "--fixture", str(SMALL)) == 0
    assert run_cli("extract", "--home", str(fenced_home), "--engine", "ollama", "--json") == 0
    conn = connect(fenced_home)
    try:
        assert conn.execute("select count(*) as count from learning_candidates").fetchone()["count"] == 1
    finally:
        conn.close()

    invalid_home = tmp_path / "invalid"
    invalid = patch_ollama(monkeypatch, FakeOllama(invalid_json=True))
    assert run_cli("sync", "--home", str(invalid_home), "--fixture", str(SMALL)) == 0
    assert run_cli("extract", "--home", str(invalid_home), "--engine", "ollama", "--json") == 0
    conn = connect(invalid_home)
    try:
        assert invalid.calls >= 2
        run = conn.execute("select * from model_runs where status='failed'").fetchone()
        assert run is not None
        assert run["error_code"] == "invalid_json"
        assert run["attempts"] == 2
        assert "not json" in (run["response_json"] or "")
        assert conn.execute("select count(*) as count from learning_candidates").fetchone()["count"] == 0
    finally:
        conn.close()

    secret = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz123456"
    db_url = "DATABASE_URL=" + "postgres" + "://user:pass@example.com/db"
    webhook_secret = "WEBHOOK_SECRET=" + "wh" + "sec_123456789"
    secret_home = tmp_path / "secret"
    secret_fixture = tmp_path / "secret.json"
    secret_fixture.write_text(
        json.dumps(
            {
                "prs": [
                    {
                        "repo_full_name": "octo/secret",
                        "number": 7,
                        "github_id": "PR_secret",
                        "title": "Stop leaking auth data",
                        "url": "https://github.com/octo/secret/pull/7",
                        "author_login": "coder",
                        "state": "OPEN",
                        "created_at": "2026-05-01T10:00:00Z",
                        "updated_at": "2026-05-01T11:00:00Z",
                        "reviews": [
                            {
                                "id": "review-secret",
                                "author": {"login": "alice-login"},
                                "state": "CHANGES_REQUESTED",
                                "body": (
                                    "Please stop logging Authorization: Bearer "
                                    + secret
                                    + f"\n{db_url}\n{webhook_secret}"
                                ),
                                "created_at": "2026-05-01T10:30:00Z",
                            }
                        ],
                    }
                ]
            }
        )
    )
    patch_ollama(monkeypatch, FakeOllama(lambda payload: {
        "candidates": [
            {
                "title": "Avoid logging authentication material",
                "lesson": "Review feedback caught authentication material being logged.",
                "prevention_rule": "Before logging request data, verify auth headers, webhook secrets, and database URLs are redacted or omitted.",
                "mistake_pattern": "secret-logging",
                "tags": ["security", "logging"],
                "severity": "critical",
                "confidence": 0.91,
                "learning_type": "security",
                "evidence_ids": [first_primary(payload)["evidence_id"]],
                "evidence_chunk_ids": [first_primary(payload)["chunk_id"]],
                "evidence_quotes": [first_primary(payload)["text"][:120]],
            }
        ]
    }))
    assert run_cli("sync", "--home", str(secret_home), "--fixture", str(secret_fixture)) == 0
    assert run_cli("extract", "--home", str(secret_home), "--engine", "ollama", "--json") == 0
    conn = connect(secret_home)
    try:
        joined_chunks = "\n".join(row["text"] for row in conn.execute("select text from evidence_chunks"))
        joined_runs = "\n".join(
            (row["request_json"] or "") + (row["response_json"] or "") for row in conn.execute("select * from model_runs")
        )
        assert secret not in joined_chunks
        assert secret not in joined_runs
        for raw_identifier in [
            "octo/secret",
            "https://github.com/octo/secret/pull/7",
            "coder",
            "alice-login",
        ]:
            assert raw_identifier not in joined_runs
        assert "[REDACTED_REPO_" in joined_runs
        assert "[REDACTED_USER_" in joined_runs
        assert "[REDACTED_" in joined_chunks
        candidate = conn.execute("select * from learning_candidates").fetchone()
    finally:
        conn.close()
    assert candidate is not None
    assert run_cli("accept-candidate", candidate["id"], "--home", str(secret_home)) == 0
    out_dir = secret_home / "exports-check"
    assert run_cli("export", "--home", str(secret_home), "--out", str(out_dir)) == 0
    assert secret not in (out_dir / "LEARNINGS.md").read_text()
    assert secret not in (out_dir / "context.json").read_text()
    assert run_cli("daily", "--home", str(secret_home), "--fixture", str(secret_fixture), "--json") == 0
    report_text = "\n".join(path.read_text() for path in (secret_home / "reports").glob("*.md"))
    assert secret not in report_text
