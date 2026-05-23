# Security Policy

`prlearn` is designed as a local-first tool. Treat the local database, exports,
logs, prompts, and scheduler output as private developer data because they can
include repository names, branch names, pull request URLs, reviewer names, file
paths, and redacted evidence snippets.

## Supported Versions

Security fixes are accepted against `main` until the project starts publishing
versioned releases.

## Secret Handling

- Do not commit `.env`, `.prlearn/`, SQLite databases, raw exports, private
  GitHub App keys, Telegram bot tokens, Codex credentials, OpenAI API keys, or
  scheduler files that contain environment values.
- Keep GitHub App private keys outside the repository, for example under
  `~/.prlearn/keys/`, with file permissions limited to the local user.
- Use `PRLEARN_PASSPHRASE` and `prlearn privacy encrypt-raw` if raw GitHub
  payloads should be encrypted at rest.
- Redact `doctor --json`, `list --json`, `preflight`, reports, and exports
  before posting support output publicly.

## Credential Rotation

Rotate any credential that was ever pasted into a chat, terminal transcript,
issue, pull request, CI log, or committed file, even if the repository scan no
longer finds it.

Recommended rotation points before a public release:

- Regenerate Telegram bot tokens through BotFather if they were shared outside
  the maintainer's private secret store.
- Regenerate GitHub App private keys if a key was downloaded into the repo,
  pasted into chat, used in a shared shell history, or stored in CI logs.
- Revoke leaked OpenAI API keys and create user-owned replacement keys.
- Revoke or refresh local Codex CLI sessions on any machine that may be
  compromised.

Each user should create their own GitHub App installation, Telegram bot,
optional OpenAI API key, optional Codex login, optional Ollama models, and local
encryption passphrase. Maintainer credentials must never be reused by public
users.

To rotate a Telegram bot token, open Telegram, message `@BotFather`, send
`/revoke`, choose the affected bot, and store only the new token in your private
local environment or secret manager. Do not paste the replacement token into
chat, issues, pull requests, commits, or CI logs.

## Public Release Gate

Before making a repository public, run:

```bash
python scripts/check_public_release.py
python scripts/check_public_release.py --strict-history
python -m pytest -q
python -m prlearn eval \
  --fixture tests/fixtures/github_small.json \
  --incremental-fixture tests/fixtures/github_incremental.json \
  --json
```

`--strict-history` is expected to fail if author or committer metadata contains
personal email addresses or private branch history. Do not flip repository
visibility until those findings are remediated or the project is published from
a clean public mirror.

## Reporting Vulnerabilities

Use a private GitHub security advisory when available. If advisories are not
enabled yet, contact the maintainer through GitHub and avoid posting exploit
details or credentials in a public issue.
