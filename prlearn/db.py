from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .config import load_config
from .util import ensure_home, utcnow, write_json

SCHEMA_VERSION = 5

MIGRATION_1 = """
create table if not exists schema_migrations(
  version integer primary key,
  applied_at text not null
);
create table if not exists sync_state(
  key text primary key,
  value text not null,
  updated_at text not null
);
create table if not exists sync_runs(
  id integer primary key autoincrement,
  started_at text not null,
  finished_at text,
  status text not null,
  query text,
  prs_seen integer default 0,
  prs_changed integer default 0,
  events_upserted integer default 0,
  learnings_created integer default 0,
  learnings_updated integer default 0,
  error text
);
create table if not exists prs(
  id integer primary key autoincrement,
  repo_full_name text not null,
  number integer not null,
  github_id text,
  title text,
  url text,
  author_login text,
  state text,
  is_merged integer default 0,
  created_at text,
  updated_at text,
  closed_at text,
  merged_at text,
  head_sha text,
  base_ref text,
  head_ref text,
  last_synced_at text,
  dirty integer default 0,
  raw_json text,
  unique(repo_full_name, number)
);
create table if not exists events(
  id integer primary key autoincrement,
  repo_full_name text not null,
  pr_number integer not null,
  external_id text not null unique,
  kind text not null,
  actor_login text,
  body text,
  normalized_body text,
  path text,
  line integer,
  commit_sha text,
  review_state text,
  check_name text,
  check_conclusion text,
  url text,
  created_at text,
  updated_at text,
  content_hash text not null,
  raw_json text
);
create table if not exists pr_files(
  id integer primary key autoincrement,
  repo_full_name text not null,
  pr_number integer not null,
  path text not null,
  status text,
  additions integer,
  deletions integer,
  changes integer,
  language text,
  raw_json text,
  unique(repo_full_name, pr_number, path)
);
create table if not exists check_runs(
  id integer primary key autoincrement,
  repo_full_name text not null,
  pr_number integer not null,
  external_id text not null unique,
  name text,
  status text,
  conclusion text,
  started_at text,
  completed_at text,
  url text,
  content_hash text not null,
  raw_json text
);
create table if not exists learning_cards(
  id integer primary key autoincrement,
  canonical_key text not null unique,
  title text not null,
  lesson text not null,
  mistake_pattern text,
  prevention_rule text not null,
  tags_json text not null,
  contexts_json text not null,
  severity integer not null default 1,
  confidence real not null default 0.5,
  status text not null default 'pending',
  recurrence_count integer not null default 1,
  first_seen_at text not null,
  last_seen_at text not null,
  created_at text not null,
  updated_at text not null
);
create table if not exists learning_evidence(
  id integer primary key autoincrement,
  learning_id integer not null,
  event_id integer,
  repo_full_name text not null,
  pr_number integer not null,
  quote text,
  url text,
  created_at text not null,
  unique(learning_id, event_id)
);
"""

MIGRATION_2 = """
create table if not exists evidence_items(
  id text primary key,
  repo_full_name text not null,
  pr_number integer not null,
  source_table text not null,
  source_id text not null,
  source_kind text not null,
  actor_login text,
  actor_role text,
  is_primary_signal integer not null default 0,
  is_self_authored integer not null default 0,
  is_bot integer not null default 0,
  is_noise integer not null default 0,
  signal_score real not null default 0,
  title text,
  url text,
  path text,
  line integer,
  created_at text not null,
  updated_at text not null,
  redacted_text text not null,
  redaction_hits_json text not null default '{}',
  metadata_json text not null default '{}',
  content_hash text not null,
  unique(source_table, source_id, source_kind)
);
create index if not exists idx_evidence_items_pr on evidence_items(repo_full_name, pr_number);
create index if not exists idx_evidence_items_primary on evidence_items(is_primary_signal, is_noise);

create table if not exists evidence_chunks(
  id text primary key,
  evidence_id text not null references evidence_items(id) on delete cascade,
  repo_full_name text not null,
  pr_number integer not null,
  chunk_index integer not null,
  chunk_kind text not null,
  text text not null,
  token_estimate integer not null default 1,
  is_primary_signal integer not null default 0,
  is_context integer not null default 1,
  is_noise integer not null default 0,
  redaction_hits_json text not null default '{}',
  metadata_json text not null default '{}',
  content_hash text not null,
  created_at text not null,
  unique(evidence_id, chunk_index, content_hash)
);
create index if not exists idx_evidence_chunks_pr on evidence_chunks(repo_full_name, pr_number);
create index if not exists idx_evidence_chunks_primary on evidence_chunks(is_primary_signal, is_noise);

create table if not exists model_runs(
  id text primary key,
  run_type text not null,
  model_name text not null,
  prompt_version text not null,
  schema_hash text not null,
  input_hash text not null,
  options_json text not null,
  request_json text not null,
  response_json text,
  status text not null,
  attempts integer not null default 1,
  error_code text,
  error text,
  created_at text not null,
  updated_at text not null,
  completed_at text
);
create index if not exists idx_model_runs_status on model_runs(status, run_type);

create table if not exists learning_candidates(
  id text primary key,
  source text not null,
  canonical_key text not null,
  title text not null,
  lesson text not null,
  mistake_pattern text,
  prevention_rule text not null,
  tags_json text not null,
  contexts_json text not null,
  severity integer not null default 1,
  confidence real not null default 0.5,
  learning_type text,
  status text not null default 'pending',
  model_run_id text references model_runs(id),
  duplicate_of_card_id integer references learning_cards(id),
  validation_errors_json text not null default '[]',
  raw_candidate_json text not null default '{}',
  created_at text not null,
  updated_at text not null,
  unique(canonical_key, source)
);
create index if not exists idx_learning_candidates_status on learning_candidates(status);

create table if not exists learning_candidate_evidence(
  candidate_id text not null references learning_candidates(id) on delete cascade,
  evidence_id text not null references evidence_items(id) on delete cascade,
  chunk_id text references evidence_chunks(id) on delete set null,
  quote text,
  role text not null default 'supporting',
  created_at text not null,
  primary key(candidate_id, evidence_id, chunk_id)
);

create table if not exists learning_card_recurrences(
  id text primary key,
  learning_id integer not null references learning_cards(id) on delete cascade,
  candidate_id text references learning_candidates(id) on delete set null,
  canonical_key text not null,
  repo_full_name text not null,
  pr_number integer not null,
  evidence_hash text not null,
  created_at text not null,
  unique(learning_id, canonical_key, repo_full_name, pr_number)
);
create index if not exists idx_learning_card_recurrences_card on learning_card_recurrences(learning_id);

create table if not exists learning_card_feedback(
  id integer primary key autoincrement,
  learning_id integer references learning_cards(id) on delete cascade,
  candidate_id text references learning_candidates(id) on delete set null,
  action text not null,
  reason text,
  created_at text not null
);

create table if not exists embeddings(
  id text primary key,
  owner_type text not null,
  owner_id text not null,
  model_name text not null,
  text_hash text not null,
  dimensions integer not null,
  vector_blob blob not null,
  metadata_json text not null default '{}',
  created_at text not null,
  unique(owner_type, owner_id, model_name, text_hash)
);
"""

MIGRATION_3 = """
create table if not exists learning_candidate_ratings(
  id integer primary key autoincrement,
  candidate_id text not null references learning_candidates(id) on delete cascade,
  learning_id integer references learning_cards(id) on delete set null,
  rating text not null check(rating in ('major', 'minor', 'not_important')),
  source text not null default 'telegram',
  telegram_user_id text,
  telegram_chat_id_hash text,
  created_at text not null,
  unique(candidate_id, source)
);
create index if not exists idx_learning_candidate_ratings_candidate on learning_candidate_ratings(candidate_id);
"""

MIGRATION_4 = """
create table if not exists telegram_candidate_messages(
  id integer primary key autoincrement,
  candidate_id text not null references learning_candidates(id) on delete cascade,
  chat_id_hash text not null,
  message_id integer not null,
  callback_nonce text not null,
  created_at text not null,
  reviewed_at text,
  unique(candidate_id, chat_id_hash, message_id),
  unique(callback_nonce)
);
create index if not exists idx_telegram_candidate_messages_candidate on telegram_candidate_messages(candidate_id);
create index if not exists idx_telegram_candidate_messages_nonce on telegram_candidate_messages(callback_nonce);
"""

MIGRATION_5 = """
create table if not exists seed_repos(
  repo_full_name text primary key,
  owner text not null,
  name text not null,
  default_branch text,
  private integer not null default 0,
  fork integer not null default 0,
  archived integer not null default 0,
  language text,
  size integer,
  open_issues_count integer,
  pushed_at text,
  updated_at text,
  score real not null default 0,
  skipped_reason text,
  last_seeded_pr_at text,
  last_seeded_commit_sha text,
  last_seeded_commit_at text,
  last_seen_at text not null,
  raw_json text not null default '{}'
);
create index if not exists idx_seed_repos_score on seed_repos(skipped_reason, score desc);
create index if not exists idx_seed_repos_last_seen on seed_repos(last_seen_at);

create table if not exists seed_commit_signals(
  id text primary key,
  repo_full_name text not null,
  sha text not null,
  message text not null,
  author_login text,
  committed_at text,
  url text,
  signal text not null,
  score real not null default 0,
  candidate_id text references learning_candidates(id) on delete set null,
  raw_json text not null default '{}',
  created_at text not null,
  updated_at text not null,
  unique(repo_full_name, sha)
);
create index if not exists idx_seed_commit_signals_repo on seed_commit_signals(repo_full_name, committed_at);
create index if not exists idx_seed_commit_signals_candidate on seed_commit_signals(candidate_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        "create table if not exists schema_migrations(version integer primary key, applied_at text not null)"
    )
    applied = {row["version"] for row in conn.execute("select version from schema_migrations")}
    if 1 not in applied:
        conn.executescript(MIGRATION_1)
        conn.execute(
            "insert or ignore into schema_migrations(version, applied_at) values(?, ?)",
            (1, utcnow()),
        )
    applied = {row["version"] for row in conn.execute("select version from schema_migrations")}
    if 2 not in applied:
        conn.executescript(MIGRATION_2)
        conn.execute(
            "insert or ignore into schema_migrations(version, applied_at) values(?, ?)",
            (2, utcnow()),
        )
    applied = {row["version"] for row in conn.execute("select version from schema_migrations")}
    if 3 not in applied:
        conn.executescript(MIGRATION_3)
        conn.execute(
            "insert or ignore into schema_migrations(version, applied_at) values(?, ?)",
            (3, utcnow()),
        )
    applied = {row["version"] for row in conn.execute("select version from schema_migrations")}
    if 4 not in applied:
        conn.executescript(MIGRATION_4)
        conn.execute(
            "insert or ignore into schema_migrations(version, applied_at) values(?, ?)",
            (4, utcnow()),
        )
    applied = {row["version"] for row in conn.execute("select version from schema_migrations")}
    if 5 not in applied:
        conn.executescript(MIGRATION_5)
        conn.execute(
            "insert or ignore into schema_migrations(version, applied_at) values(?, ?)",
            (5, utcnow()),
        )
    conn.commit()


def init_home(home: Path, db_path: Path) -> None:
    ensure_home(home)
    config = home / "config.json"
    if not config.exists():
        write_json(config, {"db": str(db_path), "created_at": utcnow(), "version": SCHEMA_VERSION})
    load_config(home, db_path)
    conn = connect(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("select value from sync_state where key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "insert into sync_state(key, value, updated_at) values(?, ?, ?) "
        "on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at",
        (key, value, utcnow()),
    )


def one(conn: sqlite3.Connection, query: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(query, tuple(params)).fetchone()
