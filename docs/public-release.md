# Public Release Checklist

This repository should not be made public by simply flipping visibility until
the release gate below is clean. Current tracked source files are intended to be
public-safe, but commit metadata and branch history still need a final
publication decision.

## Current Audit Result

Checked surfaces:

- Tracked files for common token, private key, local path, and credential URL
  patterns.
- Ignored/generated files that could accidentally be staged.
- GitHub Actions permissions and repository metadata files.
- Commit author and committer metadata across all local refs.
- Stale local and remote branches.

Current blockers before direct publication:

- Git history contains non-noreply author or committer metadata, including a
  personal email address and an early placeholder address.
- The repository has stale feature branches and historical private PR context.
- Existing private repository PR metadata may become visible if the private repo
  itself is made public.

Recommended publication path: create a clean public mirror from the sanitized
working tree, with one initial commit authored from a GitHub noreply address.
This avoids exposing private PR history, stale branch names, and personal commit
metadata.

## Repeatable Gate

Run before any public visibility change:

```bash
python scripts/check_public_release.py
python scripts/check_public_release.py --strict-history
python -m pytest -q
python -m prlearn eval \
  --fixture tests/fixtures/github_small.json \
  --incremental-fixture tests/fixtures/github_incremental.json \
  --json
```

The non-strict check should pass before merge. The strict history check should
pass only for the repository that will become public. If it fails in this
private repo, use a clean mirror or sanitize history first.

## Clean Mirror Flow

1. Keep the current private repo private.
2. Confirm `main` is clean and has passed CI.
3. Copy only the tracked working tree into a fresh directory.
4. Remove `.git`, local databases, caches, private env files, and generated
   exports.
5. Initialize a new git repo with maintainer noreply author metadata.
6. Commit the sanitized tree as the first public commit.
7. Create the public GitHub repo.
8. Enable branch protection, secret scanning, Dependabot, and code scanning.
9. Push `main` and verify CI before inviting contributors.

## Variables Users Must Create Themselves

- `PRLEARN_GITHUB_APP_ID`
- `PRLEARN_GITHUB_INSTALLATION_ID`
- `PRLEARN_GITHUB_APP_PRIVATE_KEY_FILE`
- `PRLEARN_GITHUB_AUTHOR`
- `PRLEARN_PASSPHRASE` if encrypting raw GitHub payloads.
- `PRLEARN_TELEGRAM_BOT_TOKEN` and `PRLEARN_TELEGRAM_CHAT_ID` if using Telegram
  review.
- `OPENAI_API_KEY` only when using the direct OpenAI engine.
- Codex CLI OAuth login when using the `codex` engine.
- Local Ollama models when using the `ollama` or `hybrid` engines.

## Credentials To Rotate

Repository scans did not find committed live credentials in tracked content.
Still rotate any credential that was pasted into chat, stored in local shell
history, or used in a shared transcript:

- Telegram bot tokens.
- GitHub App private keys.
- OpenAI API keys.
- GitHub personal access tokens.
- Any local passphrase that was shared outside the user's private secret store.

## Branch And Code Integrity Controls

Configure GitHub rulesets or branch protection for `main` before the repository
is public:

- Require pull requests and block direct pushes.
- Require maintainer and CODEOWNERS review.
- Require passing CI on Python 3.11 and 3.12.
- Require fixture evaluation.
- Dismiss stale approvals after new commits.
- Require conversation resolution.
- Block force pushes and branch deletion.
- Prefer squash or linear-history merges.
- Restrict who can bypass protections.
- Enable signed commits if it fits the maintainer workflow.

To reduce compromised-code risk:

- Keep GitHub Actions on read-only token permissions unless a job needs more.
- Use Dependabot for package and GitHub Actions updates.
- Enable secret scanning and push protection.
- Enable code scanning for Python when available.
- Review any new dependency, generated file, network call, credential store, or
  model-provider integration as a security-sensitive change.
- Keep fixture-based validation mandatory so provider credentials are not needed
  to verify the core behavior.
