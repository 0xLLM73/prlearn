# Local Setup

This guide is for users setting up `prlearn` on their own machine. Users should
create their own credentials; maintainer credentials are not part of the public
release.

## 1. Install

```bash
git clone https://github.com/<owner>/prlearn.git
cd prlearn
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
python -m prlearn init
python -m prlearn doctor
```

Fixture mode works without GitHub or model-provider credentials:

```bash
python -m prlearn daily --fixture tests/fixtures/github_small.json --json
python -m prlearn review --limit 10
python -m prlearn export
```

## 2. Choose GitHub Auth

GitHub App auth is recommended for unattended daily sync because the app can be
installed on selected repositories and mints short-lived installation tokens.

Create a GitHub App with repository permissions:

- Metadata: read
- Pull requests: read
- Issues: read
- Contents: read
- Checks: read

Install it on the repositories you want to learn from, then download a private
key and keep it outside the repo:

```bash
mkdir -p ~/.prlearn/keys
mv ~/Downloads/*.private-key.pem ~/.prlearn/keys/prlearn-github-app.pem
chmod 600 ~/.prlearn/keys/prlearn-github-app.pem
```

Set local environment values:

```bash
export PRLEARN_GITHUB_APP_ID="<your-app-id>"
export PRLEARN_GITHUB_INSTALLATION_ID="<your-installation-id>"
export PRLEARN_GITHUB_APP_PRIVATE_KEY_FILE="$HOME/.prlearn/keys/prlearn-github-app.pem"
export PRLEARN_GITHUB_AUTHOR="<your-github-login>"
```

Verify:

```bash
python -m prlearn doctor --json
python -m prlearn sync --github-auth app --author "$PRLEARN_GITHUB_AUTHOR" --limit 5 --json
```

GitHub CLI auth is available as a fallback with `gh auth login`, but a GitHub
App is more stable for scheduled runs.

## 3. Choose An Extraction Engine

- `heuristic`: deterministic local default; no model credentials.
- `hybrid`: tries local Ollama first, then falls back to heuristic.
- `ollama`: strict local Ollama mode.
- `codex`: hosted Codex CLI OAuth path; run `codex login`.
- `openai`: direct API path; set `OPENAI_API_KEY`.

Codex OAuth does not require an OpenAI API key:

```bash
codex login
python -m prlearn daily --github-auth app --author "$PRLEARN_GITHUB_AUTHOR" --engine codex --json
```

Ollama stays local by default:

```bash
ollama pull qwen3.5:9b
python -m prlearn daily --github-auth app --author "$PRLEARN_GITHUB_AUTHOR" --engine hybrid --json
```

## 4. Optional Telegram Review

Create your own Telegram bot with BotFather. Message the bot once, then find the
chat ID:

```bash
export PRLEARN_TELEGRAM_BOT_TOKEN="<your-bot-token>"
python -m prlearn telegram chats
```

Set the printed chat ID:

```bash
export PRLEARN_TELEGRAM_CHAT_ID="<your-chat-id>"
python -m prlearn telegram send --limit 10 --ready
python -m prlearn telegram poll --timeout 10
```

Reviewed messages are deleted after ratings are recorded unless you pass
`--keep-reviewed`.

## 5. Optional Raw Payload Encryption

```bash
export PRLEARN_PASSPHRASE="<long-local-passphrase>"
python -m prlearn privacy encrypt-raw
python -m prlearn privacy status
```

This encrypts raw GitHub payload columns. Normalized learning cards, reports,
and exports remain readable.

## 6. Daily Automation

Use GitHub App auth for a stable daily job:

```bash
python -m prlearn daily \
  --github-auth app \
  --author "$PRLEARN_GITHUB_AUTHOR" \
  --lookback-days 3 \
  --engine hybrid \
  --json
```

Do not set a low daily PR sync limit. Use `--max-model-prs` only to cap model
provider spend for that run; synced PR evidence can be processed later.

## 7. What Not To Commit

Never commit:

- `.env` or private environment files.
- `.prlearn/`, `prlearn.db`, `*.sqlite`, or generated reports/exports.
- GitHub App private keys.
- Telegram bot tokens or chat IDs.
- OpenAI API keys.
- Codex CLI credential files.
- Real `doctor --json`, `list --json`, `preflight`, report, or export output
  without redaction.
