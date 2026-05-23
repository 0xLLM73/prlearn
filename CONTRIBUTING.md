# Contributing

Thanks for helping improve `prlearn`. Keep contributions small, reviewable, and
grounded in the local-first privacy model.

## Development Workflow

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
python -m pytest -q
python -m prlearn eval \
  --fixture tests/fixtures/github_small.json \
  --incremental-fixture tests/fixtures/github_incremental.json \
  --json
```

Use fixture sync for tests unless a change specifically requires live GitHub,
Codex, OpenAI, Ollama, or Telegram credentials.

## Pull Requests

- Open a branch from current `main`.
- Keep generated files, local databases, `.env` files, private keys, and exports
  out of the diff.
- Add or update focused tests for behavior changes.
- Run the fixture evaluation before requesting review.
- Explain any new dependency, network call, provider integration, or stored data
  field in the PR description.
- Do not weaken redaction, localhost-only Ollama defaults, raw-payload
  encryption behavior, or user-review gates without a security-focused review.

## Branch Control For Maintainers

Before making the repository public, configure GitHub branch protection or a
repository ruleset for `main`:

- Require pull requests before merge.
- Require at least one maintainer review and CODEOWNERS review.
- Dismiss stale approvals after new commits.
- Require status checks for the Python test matrix and fixture evaluation.
- Require conversation resolution before merge.
- Block force pushes and branch deletion.
- Restrict direct pushes to trusted maintainers.
- Prefer squash or linear-history merges.
- Require signed commits if that fits the maintainer workflow.

Enable GitHub security features before accepting public contributions:

- Secret scanning and push protection.
- Dependabot alerts and security updates.
- Dependabot version updates for Python packages and GitHub Actions.
- Code scanning for Python if available for the account.

## Supply-Chain Expectations

New dependencies should be necessary, narrowly scoped, and pinned through the
standard Python packaging metadata. Avoid adding background network calls,
telemetry, hosted AI providers, or credential persistence without explicit docs,
tests, and an opt-in path.
