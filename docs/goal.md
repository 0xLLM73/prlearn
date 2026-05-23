# Historical MVP Goal: Build `prlearn`

This document is the original implementation goal/spec. It is useful for project history and acceptance intent, but it is not the current user guide. The README is the primary local-use guide. Current implementation includes newer surfaces such as `privacy`, `insights`, `eval`, schedule management, Ollama learning candidates, usefulness scoring, and richer preflight/export behavior.

Build a complete local-first CLI tool named `prlearn`.

`prlearn` helps vibe coders learn from their own GitHub pull requests. It syncs PR history, review feedback, comments, files, commits, and CI/check failures; extracts durable lessons; dedupes recurring mistakes; and exports a continuously updated personal coding memory.

Do not build a web app. Build a working local CLI with SQLite storage, fixtures, tests, docs, and local validation.

## Core product behavior

The tool must support a daily incremental loop:

1. Find PRs updated since the last successful sync.
2. Fetch PR details, reviews, review comments, issue-style PR comments, files, commits, and check runs.
3. Upsert everything into local SQLite.
4. Mark changed PRs as dirty.
5. Extract new learning candidates from dirty PRs.
6. Dedupe new candidates against existing learning cards.
7. Attach new evidence to existing learnings when the same mistake appears again.
8. Export updated learning files.
9. Produce a daily report.

The most important rule: do not keep creating duplicate "add tests" style lessons. Repeated feedback should update the existing learning card by increasing recurrence count and adding evidence.

## Implementation stack

Use Python 3.11+ unless this repository already clearly uses another stack.

Prefer:

- argparse or click/typer for CLI
- sqlite3 for DB
- pathlib for paths
- subprocess for GitHub CLI calls
- pytest for tests
- standard library where possible

Keep runtime dependencies minimal.

## Privacy and auth

- Local-first.
- Store data in SQLite.
- Default home: `~/.prlearn`
- Default DB: `~/.prlearn/prlearn.db`
- Allow overrides:
  - `--home`
  - `--db`
- Never store GitHub tokens in SQLite.
- Use GitHub CLI `gh` for authentication.
- Do not implement OAuth for MVP.
- Do not persist secrets.
- Redact secrets from logs.

## Required CLI commands

Implement these commands:

### `prlearn init`

Creates:

- `~/.prlearn`
- `~/.prlearn/config.json`
- `~/.prlearn/logs`
- `~/.prlearn/reports`
- `~/.prlearn/exports`
- SQLite DB
- DB migrations

Must be idempotent.

### `prlearn doctor`

Checks:

- Python version
- DB path writable
- `git` exists
- `gh` exists
- `gh auth status`
- scheduler status, if implemented

Options:

- `--json`
- `--quiet`
- `--home`
- `--db`

Do not fail hard if `gh` auth is missing. Print clear next steps.

### `prlearn sync`

Syncs PR data from GitHub or fixtures.

Options:

- `--author @me`
- `--since YYYY-MM-DD`
- `--incremental`
- `--lookback-days N`
- `--limit N`
- `--repo owner/name`, repeatable
- `--owner owner`, repeatable
- `--fixture path/to/fixture.json`
- `--dry-run`
- `--json`
- `--home`
- `--db`

Behavior:

- If `--fixture` is supplied, do not call GitHub.
- If `--incremental` is supplied, read `last_successful_sync_at` from `sync_state`, subtract `lookback_days`, and query PRs updated since that point.
- Default `lookback_days` should be 3.
- Upsert PRs, events, files, and check runs.
- Use content hashes to detect changes.
- Mark PRs dirty when relevant data changes.
- Record a `sync_runs` row.
- Running the same sync twice must not create duplicates.

### `prlearn extract`

Extracts learning candidates from dirty PRs.

Options:

- `--all`
- `--pr owner/repo#123`
- `--min-confidence 0.55`
- `--json`
- `--home`
- `--db`

Behavior:

- Use deterministic heuristic extraction for MVP.
- Do not require an LLM.
- Every learning must have evidence.
- Do not invent lessons.
- Ignore praise/noise.
- Prefer specific prevention rules over vague advice.
- Clear dirty flags after successful extraction.

### `prlearn dedupe`

Dedupes learning cards.

Behavior:

- Compute canonical keys from normalized title, rule, and tags.
- Merge repeated patterns.
- Increment recurrence count.
- Attach additional evidence.
- Do not recreate rejected cards.

### `prlearn daily`

Runs the full daily pipeline:

1. doctor
2. sync --incremental
3. extract
4. dedupe
5. export
6. report

Options:

- `--author`
- `--since`
- `--lookback-days`
- `--fixture`
- `--json`
- `--home`
- `--db`

Behavior:

- Use a lock file to prevent overlapping runs.
- Recover safely from stale lock.
- Write logs to `~/.prlearn/logs/daily.log`.
- Write report to `~/.prlearn/reports/YYYY-MM-DD.md`.
- Must be safe and idempotent.

### `prlearn list`

Lists learning cards.

Options:

- `--status accepted|pending|rejected|archived`
- `--tag TAG`
- `--repo owner/name`
- `--json`
- `--limit N`
- `--home`
- `--db`

### `prlearn review`

Interactive review of pending learning cards.

User can:

- accept
- reject
- edit title
- edit lesson
- edit prevention rule
- merge with existing card
- skip

Also implement non-interactive commands:

- `prlearn accept ID`
- `prlearn reject ID`
- `prlearn archive ID`
- `prlearn merge ID ID`

### `prlearn preflight`

Shows relevant accepted learnings before coding.

Options:

- `--repo .`
- `--task "build a Stripe webhook handler"`
- `--top 10`
- `--include-pending`
- `--json`
- `--home`
- `--db`

Behavior:

- If `--repo` points to a git repo, detect GitHub remote owner/name.
- Rank by repo match, tag match, task text match, recurrence count, severity, confidence, and recency.
- Output compact reminders, not a huge dump.

Example output:

```text
Personal PR learnings relevant to this task:

1. Verify webhook signatures before trusting payloads
   Prevention: For every webhook handler, add signature verification, replay/idempotency protection, and a negative test for invalid signatures.
   Seen in: 1 PR
   Tags: security, webhook, api, auth

2. Run typecheck before opening PRs
   Prevention: Run the project's typecheck/build command before opening a PR or after accepting generated code.
   Seen in: 1 PR
   Tags: types, ci
```

### `prlearn export`

Exports:

- `~/.prlearn/exports/LEARNINGS.md`
- `~/.prlearn/exports/context.json`
- `~/.prlearn/exports/rules.md`

Options:

- `--out path`
- `--format markdown|json|all`
- `--home`
- `--db`

`LEARNINGS.md` should include title, lesson, prevention rule, tags, recurrence count, and evidence PR links.

`context.json` should be machine-readable for agents/editors.

`rules.md` should be a compact personal coding rules file.

### `prlearn schedule`

Subcommands:

- `schedule install`
- `schedule status`
- `schedule uninstall`
- `schedule print`

Behavior:

- macOS: generate LaunchAgent plist under `~/Library/LaunchAgents/com.prlearn.daily.plist`.
- Linux: prefer user systemd timer if available; otherwise print crontab line.
- Windows: print PowerShell scheduled task command unless direct implementation is easy.
- Create daily runner script under `~/.prlearn/bin/prlearn-daily`.
- Tests must not install real schedules. Test generation only.

## SQLite schema

Implement migrations with schema versioning.

Required tables:

```sql
schema_migrations(
  version integer primary key,
  applied_at text not null
);

sync_state(
  key text primary key,
  value text not null,
  updated_at text not null
);

sync_runs(
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

prs(
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

events(
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

pr_files(
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

check_runs(
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

learning_cards(
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

learning_evidence(
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
```

## GitHub ingestion

Create a GitHubClient interface with two implementations:

### `GhCliClient`

Uses `gh`.

Fetch:

- PR discovery
- PR details
- reviews
- review comments
- issue comments
- files
- check runs for PR head SHA

Handle:

- pagination
- JSON parse errors
- missing gh
- missing auth
- command failures
- rate-limit-ish failures where visible
- no secret leakage

### `FixtureGitHubClient`

Reads fixture JSON.

Must behave like the real client shape.

Used for tests and local validation.

## Fixtures

Create `tests/fixtures/github_small.json`.

It must contain at least:

- PR with null/empty UI state review comment and missing test feedback.
- PR with webhook signature verification feedback.
- PR with typecheck failure.
- PR with only LGTM/nice work that should create no learning.

Create `tests/fixtures/github_incremental.json`.

It must contain a later PR with similar missing-test/null-state feedback.

Validation rule:

1. Run daily with `github_small.json`.
2. Run daily again with `github_small.json`.
3. Run daily with `github_incremental.json`.
4. Existing testing/null-state learning should increase recurrence/evidence instead of duplicating.

## Extraction rules

High-signal include patterns:

- missing test
- add test
- regression test
- edge case
- null
- undefined
- empty state
- error state
- validation
- auth
- authorization
- signature verification
- idempotency
- race condition
- typecheck
- lint
- build failed
- should handle
- please handle
- changes requested
- this will throw
- this can break
- security
- performance

Noise exclude patterns:

- LGTM
- looks good
- nice
- thanks
- ship it
- approved
- formatting-only comments
- nit, unless paired with concrete risk

Suggested tags:

- tests
- ui-state
- null-handling
- error-handling
- api
- auth
- security
- webhook
- idempotency
- types
- ci
- lint
- performance
- architecture
- docs

Example extraction:

Input:

```text
This will throw when displayName is null. Please handle the null/empty state and add a test.
```

Learning:

```json
{
  "title": "Handle null and empty UI states before merging",
  "lesson": "UI changes should explicitly handle null, empty, loading, error, and success states when applicable.",
  "mistake_pattern": "Changed UI logic without guarding against null or empty data.",
  "prevention_rule": "Before opening a UI PR, check null, empty, loading, error, and success states and add at least one regression test for changed behavior.",
  "tags": ["ui-state", "null-handling", "tests"],
  "confidence": 0.85
}
```

Input:

```text
Please verify the Stripe signature before trusting this payload.
```

Learning:

```json
{
  "title": "Verify webhook signatures before trusting payloads",
  "lesson": "Webhook handlers must verify provider signatures before parsing or trusting request payloads.",
  "mistake_pattern": "Accepted webhook payloads without signature verification.",
  "prevention_rule": "For every webhook handler, add signature verification, replay/idempotency protection, and a negative test for invalid signatures.",
  "tags": ["security", "webhook", "api", "auth"],
  "confidence": 0.9
}
```

Input:

```text
typecheck failed
```

Learning:

```json
{
  "title": "Run typecheck before opening PRs",
  "lesson": "AI-generated code can look correct but fail concrete project type constraints.",
  "mistake_pattern": "Opened or iterated on a PR while typecheck was failing.",
  "prevention_rule": "Run the project typecheck/build command before opening a PR or after accepting generated code.",
  "tags": ["types", "ci"],
  "confidence": 0.65
}
```

## Dedupe rules

Canonicalize by:

- lowercasing
- stripping punctuation
- removing stop words
- normalizing tags
- mapping synonyms

Synonyms:

- "add test", "missing test", "regression test" -> tests
- "null", "undefined", "empty" -> null-empty-state when UI-related
- "verify signature", "signature verification" -> webhook-signature-verification
- "typecheck", "tsc", "type error" -> types

If a candidate maps to an existing canonical key:

- update recurrence count
- update last_seen_at
- update confidence if stronger
- attach new evidence
- do not create duplicate card

Rejected cards stay rejected and should not be recreated.

## Required tests

Use pytest.

Unit tests:

- config path resolution
- DB migrations are idempotent
- init creates expected files
- sync_state stores and retrieves timestamps
- PR upsert is idempotent
- event upsert is idempotent
- content_hash changes when body changes
- dirty flag is set when new evidence arrives
- dirty flag is cleared after extraction
- GitHub remote parsing:
  - `git@github.com:owner/repo.git`
  - `https://github.com/owner/repo.git`
  - `https://github.com/owner/repo`
- review comment normalization
- noise filtering
- learning extraction for tests/null state
- learning extraction for webhook signature verification
- learning extraction for CI typecheck failure
- no learning created for LGTM/nice work
- canonical_key generation
- dedupe increments recurrence instead of duplicating
- rejected learning is not recreated
- list command JSON output
- export markdown snapshot
- export JSON structure
- preflight ranking
- schedule file generation
- lock file prevents concurrent daily runs
- stale lock recovery

Integration tests:

- `prlearn init` with temp home/db
- `prlearn daily --fixture tests/fixtures/github_small.json`
- Assert DB contains expected PRs/events/learnings/evidence
- Assert exports exist
- Run same daily command again
- Assert no duplicates
- Run daily with `github_incremental.json`
- Assert existing testing card recurrence/evidence increases
- Run preflight with task "build a Stripe webhook handler"
- Assert webhook/security card appears
- Run list `--json`
- Validate JSON parseability
- Run export `--out tempdir`
- Validate generated files

Optional live test:

- Only run if `PRLEARN_LIVE=1`
- Requires gh auth
- Run limited live sync
- Skip by default

## Local validation commands

Run these before claiming completion:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
python -m prlearn --help
python -m prlearn init --home ./tmp/prlearn-home --db ./tmp/prlearn.db
python -m prlearn doctor --home ./tmp/prlearn-home --db ./tmp/prlearn.db --json
python -m prlearn daily --fixture tests/fixtures/github_small.json --home ./tmp/prlearn-home --db ./tmp/prlearn.db --json
python -m prlearn list --home ./tmp/prlearn-home --db ./tmp/prlearn.db --json
python -m prlearn export --home ./tmp/prlearn-home --db ./tmp/prlearn.db --out ./tmp/exports
python -m prlearn preflight --home ./tmp/prlearn-home --db ./tmp/prlearn.db --task "build a Stripe webhook handler" --top 5
python -m prlearn daily --fixture tests/fixtures/github_small.json --home ./tmp/prlearn-home --db ./tmp/prlearn.db --json
python -m prlearn daily --fixture tests/fixtures/github_incremental.json --home ./tmp/prlearn-home --db ./tmp/prlearn.db --json
pytest -q
```

If ruff is configured:

```bash
ruff check .
```

If mypy is configured:

```bash
mypy src prlearn
```

Do not claim success until tests pass. If setup is broken, fix setup. If live GitHub auth is missing, skip only the live test and document how to run it.

## README requirements

Write `README.md` with:

- What prlearn does
- Privacy model
- Installation
- GitHub CLI auth requirement
- Quickstart
- Daily run
- Scheduling
- Commands
- Example output
- How to use preflight before coding
- How to export rules for AI coding tools
- How tests work
- How to run optional live smoke test

## Acceptance criteria

The project is complete only when:

- CLI imports without errors
- `prlearn --help` works
- `prlearn init` is idempotent
- `prlearn daily --fixture ...` works from an empty DB
- Re-running daily with the same fixture creates no duplicates
- Running daily with incremental fixture updates an existing learning instead of duplicating it
- Every learning has evidence
- Exports are generated
- Preflight gives a compact useful list
- Tests pass
- README is clear
- No token or secret is stored
- The app can run daily and continue adding evidence to prior learnings

## Preferred module layout

Use this layout unless a better one is necessary:

```text
prlearn/
  __init__.py
  __main__.py
  cli.py
  config.py
  db.py
  github_client.py
  sync.py
  extract.py
  dedupe.py
  export.py
  preflight.py
  schedule.py
  locking.py
  models.py

tests/
  fixtures/
    github_small.json
    github_incremental.json
```

Also create:

- `pyproject.toml`
- `README.md`
- `docs/goal.md`

## Implementation guidance

- Prefer simple, reliable code over clever code.
- Use transactions around sync/extract updates.
- Use UTC ISO timestamps.
- Use deterministic ordering in exports and tests.
- Make JSON output stable and parseable.
- Use fixtures to complete the project without credentials.
- Do not ask the user questions unless live GitHub credentials are required.
