# prlearn

`prlearn` is a local-first CLI that learns from your GitHub pull requests. It stores PRs, review feedback, comments, files, commits, check failures, extracted learning cards, and evidence in SQLite, then exports compact personal coding memory files for agents and editors.

Runtime dependencies are intentionally minimal: Python 3.11+, SQLite from the standard library, and `git`. Live sync can use a stable GitHub App installation or the GitHub CLI `gh` fallback. Fixture sync and the default heuristic extractor work without GitHub credentials, Ollama, Codex, or an OpenAI API key.

## Install for local development

For a full first-run guide, see [docs/local-setup.md](docs/local-setup.md). For
maintainer publication checks, see
[docs/public-release.md](docs/public-release.md) and [SECURITY.md](SECURITY.md).

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
```

You can also run it directly from the repo:

```bash
python -m prlearn --help
python -m pytest -q
```

The only runtime package dependency is `cryptography`; the `test` extra adds `pytest`.

If your system has a `python` command that points to Python 3.11 or newer, you can use `python -m ...` after activating the venv. The commands below use `python` inside the venv.

Copy [.env.example](.env.example) only as a template. Keep real environment
values in your shell, scheduler, or private secret manager, not in git.

## Daily local loop

```bash
python -m prlearn init
python -m prlearn doctor
python -m prlearn daily --fixture tests/fixtures/github_small.json
python -m prlearn list
python -m prlearn export
```

The default home is `~/.prlearn` and the default database is `~/.prlearn/prlearn.db`. Every command accepts `--home` and `--db` overrides.

## Useful in 10 minutes

Use this loop when you want `prlearn` to help before coding or reviewing:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
python -m prlearn init
python -m prlearn doctor
python -m prlearn sync --author @me --limit 50
python -m prlearn extract --engine heuristic
python -m prlearn review --limit 10
python -m prlearn accept <id>
python -m prlearn preflight
python -m prlearn export
```

Keep the deterministic `heuristic` engine as the first local path. Use `hybrid`, `ollama`, `codex`, or `openai` only after the fixture workflow is passing and the chosen model provider is available.

For fixture-only validation without GitHub:

```bash
python -m prlearn daily --fixture tests/fixtures/github_small.json
python -m prlearn daily --fixture tests/fixtures/github_small.json
python -m prlearn daily --fixture tests/fixtures/github_incremental.json
python -m prlearn review --limit 10
python -m prlearn preflight --task "build a Stripe webhook handler"
python -m prlearn export
```

`preflight` is the main day-to-day command. It detects the current git repo, branch, changed files, optional paths, and task text, then ranks accepted lessons by specificity, recurrence, evidence quality, repo/path/language/topic match, recency, and generic-card penalties.

## Commands

- `prlearn init` creates the home directory, config, logs, reports, exports, bin directory, SQLite DB, and schema migrations. It is idempotent.
- `prlearn doctor` checks Python, DB writability, `git`, GitHub App auth, `gh`, `gh auth status`, optional Ollama reachability/model availability, optional Codex login, optional OpenAI API key configuration, optional `sqlite-vec`, and scheduler status. Missing GitHub auth or model-provider auth is reported with next steps but does not block fixture or heuristic workflows.
- `prlearn sync` syncs from GitHub or a fixture. Use `--fixture path` to avoid live GitHub calls. Live `--incremental` sync uses `sync_state.last_successful_sync_at` with a default 3-day lookback. Fixture incremental replay stays deterministic unless you pass an explicit `--since`.
- `prlearn seed` inventories accessible repositories, prioritizes active repos, backfills PRs first, optionally scans commit history for high-signal fixes/reverts/security changes, stores resumable repo cursors, and creates reviewable candidates.
- `prlearn extract` creates learning output from dirty PR evidence. Use `--engine heuristic|ollama|hybrid|codex|openai` or the alias `--provider`.
- `prlearn dedupe` merges repeated learning patterns by canonical key and attaches new evidence instead of creating duplicate cards.
- `prlearn daily` runs doctor, incremental sync, extract, dedupe, export, and report with a lock file and daily log. It finishes with a concise summary and next command. Use `--engine heuristic|ollama|hybrid|codex|openai` or `--provider`.
- `prlearn compare` runs the same fixture PR corpus through multiple engines and prints comparable cards/candidates.
- `prlearn compare-live` runs live GitHub account seeding in isolated homes for multiple engines and prints comparable candidates without mixing provider state.
- `prlearn privacy status|encrypt-raw|decrypt-raw` inspects or encrypts sensitive stored raw GitHub payloads.
- `prlearn list` lists learning cards with status, tag, repo, JSON, limit, accepted/pending/rejected/archive shortcuts, usefulness scores, and verbose evidence summaries.
- `prlearn review`, `accept`, `reject`, `archive`, and `merge` manage card status. `accept-candidate`, `reject-candidate`, and `merge-candidate` manage model learning candidates. `review --json`, `--ready`, and `--min-score` keep review scriptable.
- `prlearn telegram status|send|poll|rate` sends pending learning candidates to Telegram and records ratings as major, minor, or not important.
- `prlearn preflight [paths...] --paths ... --verbose --json` ranks relevant accepted or pending learnings before coding.
- `prlearn insights` reports whether `prlearn` is learning useful things: accepted/pending counts, recurring lessons, generic cards, weak-evidence cards, ready candidates, and suggested next commands. Add `--actionable` to route learnings into "do before next PR", "recurring risks", "needs human review", and "low-value or stale" buckets.
- `prlearn export` writes `LEARNINGS.md`, `context.json`, and `rules.md`. The markdown memory file is concise, accepted-only by default, grouped by category, and avoids evidence dumps.
- `prlearn schedule` prints or installs a daily scheduler definition.

## Preflight examples

```bash
python -m prlearn preflight
python -m prlearn preflight src/foo.py
python -m prlearn preflight --paths src/foo.py tests/test_foo.py
python -m prlearn preflight --task "add an API webhook handler"
python -m prlearn preflight --verbose
python -m prlearn preflight --json
```

Default output is intentionally short: title, why it matters, why it was selected, and a compact checklist. `--verbose` adds redacted evidence snippets and PR references. `--json` returns the detected git context and ranked lessons for editor/agent integrations.

## Extraction engines

- `heuristic` is the default and preserves the original deterministic fixture workflow.
- `hybrid` attempts Ollama candidate extraction first, then falls back to the heuristic path if Ollama is unavailable.
- `ollama` is strict: missing Ollama or a missing configured model returns a clear non-zero error.
- `codex` is strict: it invokes `codex exec` non-interactively through the local Codex CLI login and stores the output as reviewable candidates.
- `openai` is strict: it calls the OpenAI Responses API with structured JSON output and stores the result as reviewable candidates.

By default, Ollama extraction only runs when `prlearn` finds primary learning evidence such as review feedback, failed checks, high-signal PR context, or check annotations. If your PR history has sparse review feedback and you want reviewable model suggestions from PR bodies, patches, and check context, opt in explicitly:

```bash
python -m prlearn extract --engine hybrid --allow-context-only
python -m prlearn review --candidates --limit 10
```

Context-only candidates are still pending review; they are not exported until accepted.

## Actionable insights

Use `insights --actionable` after sync/extract/review to see what should affect behavior now:

```bash
python -m prlearn insights --actionable
python -m prlearn insights --actionable --json
```

The actionable report groups output into:

- `do_before_next_pr`: accepted rules that should influence preflight and the next PR.
- `recurring_risks`: patterns seen more than once, promoted even if the individual event looked minor.
- `needs_human_review`: pending cards or candidates that need accept/reject/merge decisions.
- `low_value`: generic, weak-evidence, rejected, or stale items to clean up.

Telegram ratings and review actions feed back into this ranking. A major rating increases actionability, a minor rating keeps the item useful but lower priority, and a not-important rating pushes the item toward low-value cleanup.

Ollama settings live in `~/.prlearn/config.json` under the `ollama` key. Defaults use `http://127.0.0.1:11434`, require localhost, use `qwen3.5:9b` as the extractor model, and set deterministic model options. With `require_localhost=true`, only `localhost`, `127.0.0.1`, and `::1` are accepted. All-interface or remote URLs such as `0.0.0.0` require explicitly setting `require_localhost=false`. `doctor` reports installed/missing models and suggests commands such as `ollama pull qwen3.5:9b`.

Ollama output is stored as reviewable `learning_candidates`, not direct accepted cards. Exports include accepted `learning_cards` by default; pending or rejected candidates do not appear in `rules.md`.

Ollama is optional. `heuristic` works without Ollama. `hybrid` falls back to heuristic if local Ollama is unavailable. `ollama` is strict and fails clearly if the local model is missing.

## Stable GitHub App sync

For unattended daily sync, prefer GitHub App installation auth over `gh auth`. A GitHub App can be installed on selected repositories, uses fine-grained permissions, and lets `prlearn` mint short-lived installation tokens on demand.

Create a GitHub App in GitHub with these repository permissions:

- Metadata: read
- Pull requests: read
- Issues: read
- Contents: read
- Checks: read

After creating the app, install it on the repositories you want `prlearn` to learn from and generate a private key. The app ID is shown on the app's settings page. The installation ID is the numeric ID in the installation settings URL after you install the app.

Store the private key outside the repo, for example:

```bash
mkdir -p ~/.prlearn/keys
mv ~/Downloads/*.private-key.pem ~/.prlearn/keys/prlearn-github-app.pem
chmod 600 ~/.prlearn/keys/prlearn-github-app.pem
```

Configure the daily environment:

```bash
export PRLEARN_GITHUB_APP_ID="123456"
export PRLEARN_GITHUB_INSTALLATION_ID="98765432"
export PRLEARN_GITHUB_APP_PRIVATE_KEY_FILE="$HOME/.prlearn/keys/prlearn-github-app.pem"
export PRLEARN_GITHUB_AUTHOR="your-github-login"
```

`PRLEARN_GITHUB_AUTHOR` is required when commands use the default `--author @me`, because installation tokens are repository-scoped and do not represent a specific user. You can also pass `--author your-github-login`.

Verify the connection:

```bash
python -m prlearn doctor --json
python -m prlearn sync --github-auth app --author your-github-login --limit 5 --json
```

`--github-auth auto` is the default: it uses GitHub App auth when the required app settings are present, otherwise it falls back to `gh`. Use `--github-auth app` in daily automation if you want failures to be explicit instead of silently falling back.

## Account seeding

Use `seed` for an initial account backfill or an occasional deeper refresh. It first inventories repositories available to the configured GitHub identity, skips archived/fork/inactive repos by default, scores the remaining repos by recent activity, then backfills PR evidence before using commit messages as a secondary signal. Commit-derived items are stored as pending `learning_candidates` with evidence, so they are reviewable before they become exported rules.

Start with a bounded fixture smoke:

```bash
python -m prlearn seed --fixture tests/fixtures/github_seed.json --engine heuristic --json
python -m prlearn review --candidates --json
```

For a live GitHub App backfill:

```bash
python -m prlearn seed \
  --github-auth app \
  --author your-github-login \
  --lookback-days 180 \
  --max-repos 25 \
  --max-prs 500 \
  --max-commits-per-repo 50 \
  --engine hybrid \
  --allow-context-only \
  --json
```

The caps are safety bounds for one run, not daily learning limits. For a busy day, daily automation should use `prlearn daily` without a low `--limit`; `seed` can be rerun later with a wider `--lookback-days`, `--include-inactive`, `--include-forks`, or specific `--repo owner/name` filters when you want older history.

For model-provider backfills, `--max-model-prs` limits only the number of PRs sent to the model in that run. It does not limit GitHub sync, repository inventory, commit seeding, or future daily runs.

## Codex extraction

Codex is the hosted no-API-key provider path. It uses the local Codex CLI login, so `codex login` can authenticate through the ChatGPT/Codex browser OAuth flow and cache the refreshed credentials locally. No `OPENAI_API_KEY` is required for the `codex` engine.

Codex settings live in `~/.prlearn/config.json` under the `codex` key. Defaults use the `codex` command, the user's existing Codex CLI login, `gpt-5.5`, and low reasoning effort. Set `PRLEARN_CODEX_MODEL` or `codex.model` to change the model, and set `PRLEARN_CODEX_REASONING_EFFORT` or `codex.reasoning_effort` to change reasoning. Codex requests use the same JSON schema and reviewable candidate storage as Ollama. Unlike Ollama, Codex prompts keep repo, PR, path, URL, and actor metadata readable by default because the provider already has code-writing context; credential-shaped secrets are still scrubbed before requests and storage.

To compare the default 5 fixture PRs across deterministic heuristics, local Ollama, and Codex:

```bash
python -m prlearn compare --engines heuristic,ollama,codex --json
```

The comparison command runs each engine in an isolated home and reports cards, candidates, counts, and provider errors independently. Use `--keep-home` if you want to inspect each provider's SQLite database after the run.

To compare live account output through the no-key Codex OAuth path:

```bash
python -m prlearn compare-live \
  --github-auth app \
  --author your-github-login \
  --lookback-days 14 \
  --max-repos 3 \
  --max-prs 50 \
  --max-model-prs 5 \
  --engines heuristic,ollama,codex \
  --allow-context-only \
  --keep-home \
  --json
```

## OpenAI extraction

The direct `openai` engine is optional and only for environments that already have an OpenAI API key. If you want the website OAuth flow instead, use the `codex` engine above.

OpenAI settings live in `~/.prlearn/config.json` under the `openai` key. Defaults use `OPENAI_API_KEY`, `https://api.openai.com/v1`, `gpt-5.5`, low reasoning effort, and 2048 max output tokens. The `openai` engine uses the same prompt, JSON schema, candidate validation, and per-PR candidate cache as Ollama and Codex.

```bash
export OPENAI_API_KEY="..."
python -m prlearn doctor --json
python -m prlearn extract --engine openai --max-model-prs 5 --json
python -m prlearn review --candidates --json
```

To compare the direct OpenAI API provider without mixing provider databases:

```bash
python -m prlearn compare-live \
  --github-auth app \
  --author your-github-login \
  --lookback-days 14 \
  --max-repos 3 \
  --max-prs 50 \
  --max-model-prs 5 \
  --engines heuristic,ollama,openai \
  --allow-context-only \
  --keep-home \
  --json
```

## Sustainable daily automation

For a daily unattended learning job, use GitHub App auth and avoid a low PR sync limit. A practical default is:

```bash
python -m prlearn daily \
  --github-auth app \
  --author your-github-login \
  --lookback-days 3 \
  --engine hybrid \
  --json
```

If you have an OpenAI API key and want direct API provider coverage in the daily job, use `--engine openai --max-model-prs N` where `N` is your daily budget for model-reviewed PRs. The sync still records every changed PR, and later runs can process the backlog with a wider `--max-model-prs` or a targeted `--repo owner/name` backfill.

If you do not have an OpenAI API key, use the Codex OAuth path instead:

```bash
codex login
python -m prlearn daily \
  --github-auth app \
  --author your-github-login \
  --lookback-days 3 \
  --engine codex \
  --max-model-prs 5 \
  --json
```

The sync still records every changed PR; `--max-model-prs` only controls how many PRs are sent to Codex in that run.

## Telegram candidate review

Telegram review is optional and uses the Bot API through local CLI commands. It does not store the bot token in SQLite. Set credentials in your shell:

```bash
export PRLEARN_TELEGRAM_BOT_TOKEN="..."
export PRLEARN_TELEGRAM_CHAT_ID="..."
python -m prlearn telegram status
```

Create the bot with BotFather and send the bot one message from the target chat. If you do not know the chat ID yet, run:

```bash
export PRLEARN_TELEGRAM_BOT_TOKEN="..."
python -m prlearn telegram chats
```

Use the printed chat ID as `PRLEARN_TELEGRAM_CHAT_ID`. Keep both values out of committed files.

Send pending candidates with inline rating buttons:

```bash
python -m prlearn telegram send --limit 10 --ready
```

After tapping a button in Telegram, poll once to apply the rating:

```bash
python -m prlearn telegram poll --timeout 10
```

Reviewed Telegram messages are deleted after the rating is recorded, so completed reviews do not build up in the chat. Use `--keep-reviewed` if you want to keep those messages for debugging.

Ratings map to product actions:

- `Major learning`: accepts the candidate and marks the resulting card as high severity.
- `Minor learning`: accepts the candidate and marks the resulting card as low severity.
- `Not important`: rejects the candidate with a Telegram rating reason.

For local testing or manual rating without Telegram network calls:

```bash
python -m prlearn telegram rate <candidate_id> major
python -m prlearn telegram rate <candidate_id> minor
python -m prlearn telegram rate <candidate_id> not_important
```

## Raw payload encryption

By default, `prlearn` keeps normalized learning data readable and stores GitHub raw JSON payloads as plaintext so fixture development is simple. To hide raw PR payloads, patches, review payloads, and check payloads at rest, set a passphrase and encrypt the raw columns:

```bash
export PRLEARN_PASSPHRASE="use-a-long-local-passphrase"
python -m prlearn privacy encrypt-raw
python -m prlearn privacy status
```

After `encrypt-raw`, future sync and extraction runs keep `prs.raw_json`, `events.raw_json`, `pr_files.raw_json`, and `check_runs.raw_json` encrypted. Commands that need those raw payloads require the same `PRLEARN_PASSPHRASE`, or `--prompt-passphrase`.

You usually do not need to decrypt the database to research old PRs; keep it encrypted and unlock per command with the passphrase. `privacy decrypt-raw` is available as a recovery/export escape hatch and turns raw payload storage back to plaintext in `config.json`.

This is field-level encryption for raw GitHub payloads, not full SQLite database encryption. Learning cards, normalized columns, reports, and exports remain readable unless a later SQLCipher-style full database lock is added.

Exports, reports, prompts, and preflight output use normalized/redacted learning evidence. They do not read or print encrypted raw payload blobs.

## Troubleshooting

- `python: command not found`: create a venv with `python3 -m venv .venv`, activate it, then use `python -m ...` inside the venv.
- `ModuleNotFoundError: cryptography`: install the package with `python -m pip install -e .` or install test dependencies with `python -m pip install -e ".[test]"`.
- `No module named pytest`: install the test extra with `python -m pip install -e ".[test]"`.
- `git` missing: install Git and rerun `python -m prlearn doctor`.
- GitHub App unavailable: check `PRLEARN_GITHUB_APP_ID`, `PRLEARN_GITHUB_INSTALLATION_ID`, `PRLEARN_GITHUB_APP_PRIVATE_KEY_FILE`, and `PRLEARN_GITHUB_AUTHOR`, then run `python -m prlearn doctor --json`.
- `gh` missing or unauthenticated: configure a GitHub App for stable sync, or install GitHub CLI and run `gh auth login`. Fixture sync and heuristic extraction still work without GitHub auth.
- Fixture path errors: run commands from the repository root or pass an absolute fixture path.
- Ollama unavailable or model missing: use `--engine heuristic` for the deterministic local path, or install Ollama and pull the models suggested by `python -m prlearn doctor --json`.
- Codex unavailable or unauthenticated: install the Codex CLI, run `codex login`, then rerun `python -m prlearn doctor --json`.
- OpenAI unavailable or unauthenticated: set the environment variable named by `openai.api_key_env` in `~/.prlearn/config.json` and rerun `python -m prlearn doctor --json`.
- Telegram bot errors: confirm `PRLEARN_TELEGRAM_BOT_TOKEN` and `PRLEARN_TELEGRAM_CHAT_ID` are set, that you have messaged the bot at least once, and that you run `telegram poll` after tapping rating buttons.
- Missing `PRLEARN_PASSPHRASE`: set it before encrypted raw-payload operations, or pass `--prompt-passphrase` on privacy commands that support prompting.
- Stale daily lock: `daily` removes stale locks when the recorded process is gone. If a lock remains after a crash, confirm no `prlearn daily` process is running before deleting `~/.prlearn/prlearn.lock`.
- Sharing support output: redact `doctor --json`, live `list --json`, preflight, reports, and exports before posting them publicly. They can include repository names, branch names, PR URLs, paths, actor names, or local environment details.

## Fixture validation

The fixture acceptance loop is:

```bash
TMP_HOME="$(mktemp -d)"
python -m prlearn daily --home "$TMP_HOME" --fixture tests/fixtures/github_small.json --json
python -m prlearn daily --home "$TMP_HOME" --fixture tests/fixtures/github_small.json --json
python -m prlearn daily --home "$TMP_HOME" --fixture tests/fixtures/github_incremental.json --json
python -m prlearn list --home "$TMP_HOME" --json
```

The second run must not create duplicate events or learning cards. The incremental run should increase the recurrence/evidence count for the existing null/empty-state testing lesson.

You can run the same deterministic quality gate directly:

```bash
python -m prlearn eval \
  --fixture tests/fixtures/github_small.json \
  --incremental-fixture tests/fixtures/github_incremental.json \
  --json
```

`prlearn eval` runs the fixture loop in an isolated home, verifies expected card count, idempotency, recurrence behavior, expected lesson titles, and export generation. CI runs this command after the test suite.

Run the full local validation suite:

```bash
python -m pytest -q
python -m prlearn eval \
  --fixture tests/fixtures/github_small.json \
  --incremental-fixture tests/fixtures/github_incremental.json \
  --json
```
