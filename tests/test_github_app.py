from __future__ import annotations

from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from prlearn.cli import client_for
from prlearn.config import default_config, github_config
from prlearn.github import GitHubAppClient, GitHubClientError, github_app_client_from_config, github_app_configured, normalize_private_key


def private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


class FakeGitHubAppClient(GitHubAppClient):
    def __init__(self) -> None:
        super().__init__(
            app_id="123",
            installation_id="456",
            private_key_pem=private_key_pem(),
            default_author="coder",
        )
        self.calls: list[tuple[str, str, dict[str, str] | None]] = []

    def _request_json(self, method: str, path: str, *, token: str, payload: dict[str, Any] | None = None, params: dict[str, str] | None = None) -> Any:
        self.calls.append((method, path, params))
        if method == "POST" and path == "/app/installations/456/access_tokens":
            return {"token": "installation-token", "expires_at": "2099-01-01T00:00:00Z"}
        if path == "/installation/repositories":
            return {"repositories": [{"full_name": "octo/app"}, {"full_name": "octo/payments"}]}
        if path == "/repos/octo/app/pulls":
            return [
                pull(1, "Render profile settings", "2026-05-02T12:00:00Z", "coder"),
                pull(9, "Other user change", "2026-05-03T12:00:00Z", "someone-else"),
            ]
        if path == "/repos/octo/payments/pulls":
            return [pull(2, "Add Stripe webhook handler", "2026-05-04T12:00:00Z", "coder")]
        if path == "/repos/octo/app/pulls/1":
            return pull(1, "Render profile settings", "2026-05-02T12:00:00Z", "coder")
        if path == "/repos/octo/payments/pulls/2":
            return pull(2, "Add Stripe webhook handler", "2026-05-04T12:00:00Z", "coder", head_sha="sha-pay")
        if path.endswith("/reviews"):
            return [{"id": 10, "user": {"login": "reviewer"}, "state": "CHANGES_REQUESTED", "body": "Please add tests", "html_url": "https://example/review", "submitted_at": "2026-05-02T11:00:00Z"}]
        if path.endswith("/comments") and "/issues/" in path:
            return [{"id": 11, "user": {"login": "reviewer"}, "body": "issue comment", "html_url": "https://example/issue", "created_at": "2026-05-02T11:01:00Z"}]
        if path.endswith("/comments") and "/pulls/" in path:
            return [{"id": 12, "user": {"login": "reviewer"}, "body": "review comment", "path": "src/Profile.tsx", "line": 42, "html_url": "https://example/review-comment", "created_at": "2026-05-02T11:02:00Z"}]
        if path.endswith("/files"):
            return [{"filename": "src/Profile.tsx", "status": "modified", "patch": "@@ -1 +1 @@"}]
        if path.endswith("/commits"):
            return [{"sha": "sha-commit", "commit": {"message": "Add profile settings"}}]
        if path.endswith("/timeline"):
            return [{"id": 13, "event": "ready_for_review", "actor": {"login": "coder"}, "created_at": "2026-05-02T11:03:00Z"}]
        if path.endswith("/issues/1") or path.endswith("/issues/2"):
            return {"labels": [{"name": "test"}]}
        if path.endswith("/commits/sha-pay/check-runs"):
            return {"check_runs": [{"id": 14, "name": "typecheck", "status": "completed", "conclusion": "failure", "html_url": "https://example/check", "output": {"summary": "failed"}}]}
        if path.endswith("/check-runs/14/annotations"):
            return [{"id": 15, "annotation_level": "failure", "path": "api/webhook.ts", "message": "failed", "start_line": 8}]
        if "/check-runs" in path:
            return {"check_runs": []}
        return []


def pull(number: int, title: str, updated_at: str, author: str, *, head_sha: str = "sha-app") -> dict[str, Any]:
    repo = "octo/payments" if "Stripe" in title else "octo/app"
    return {
        "id": number,
        "node_id": f"PR_{number}",
        "number": number,
        "title": title,
        "body": "body",
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "user": {"login": author},
        "state": "closed",
        "created_at": "2026-05-01T10:00:00Z",
        "updated_at": updated_at,
        "closed_at": updated_at,
        "merged_at": updated_at,
        "head": {"sha": head_sha, "ref": f"head-{number}"},
        "base": {"ref": "main"},
        "mergeable": True,
    }


def test_github_app_client_fetches_prs_with_installation_token_and_author_filter() -> None:
    client = FakeGitHubAppClient()
    prs = client.fetch_prs({"author": "@me", "limit": 10, "repos": [], "owners": [], "since": "2026-05-01T00:00:00Z"})
    assert [f"{pr['repo_full_name']}#{pr['number']}" for pr in prs] == ["octo/payments#2", "octo/app#1"]
    assert prs[1]["reviews"][0]["author"]["login"] == "reviewer"
    assert prs[1]["review_comments"][0]["path"] == "src/Profile.tsx"
    assert prs[1]["files"][0]["path"] == "src/Profile.tsx"
    assert prs[0]["check_runs"][0]["annotations"][0]["path"] == "api/webhook.ts"
    token_calls = [call for call in client.calls if call[1] == "/app/installations/456/access_tokens"]
    assert len(token_calls) == 1


class RetryGitHubAppClient(GitHubAppClient):
    def __init__(self) -> None:
        super().__init__(app_id="123", installation_id="456", private_key_pem=private_key_pem())
        self.calls = 0
        self.refreshes = 0
        self._token = "expired-token"

    def installation_token(self) -> str:
        self.refreshes += 1
        self._token = f"fresh-token-{self.refreshes}"
        return self._token


def test_github_app_client_retries_installation_token_after_401() -> None:
    class Client(RetryGitHubAppClient):
        def _request_json_once(
            self,
            method: str,
            path: str,
            *,
            token: str,
            payload: dict[str, Any] | None = None,
            params: dict[str, str] | None = None,
        ) -> Any:
            self.calls += 1
            if self.calls == 1:
                raise GitHubClientError("GitHub API 401 for GET /repos/octo/app/pulls: Bad credentials")
            return {"token": token}

    client = Client()
    assert client._request_json("GET", "/repos/octo/app/pulls", token="expired-token") == {"token": "fresh-token-1"}
    assert client.calls == 2
    assert client.refreshes == 1


def test_github_app_config_reads_env_and_key_file(tmp_path: Path, monkeypatch) -> None:
    key_file = tmp_path / "app.pem"
    key_file.write_text(private_key_pem())
    monkeypatch.setenv("PRLEARN_GITHUB_APP_ID", "123")
    monkeypatch.setenv("PRLEARN_GITHUB_INSTALLATION_ID", "456")
    monkeypatch.setenv("PRLEARN_GITHUB_APP_PRIVATE_KEY_FILE", str(key_file))
    monkeypatch.setenv("PRLEARN_GITHUB_AUTHOR", "coder")
    config = github_config(default_config(tmp_path / "prlearn.db"))
    assert github_app_configured(config)
    client = github_app_client_from_config(config)
    assert client.app_id == "123"
    assert client.installation_id == "456"
    assert client.default_author == "coder"


def test_cli_client_for_prefers_configured_github_app(tmp_path: Path, monkeypatch) -> None:
    key_file = tmp_path / "app.pem"
    key_file.write_text(private_key_pem())
    monkeypatch.setenv("PRLEARN_GITHUB_APP_ID", "123")
    monkeypatch.setenv("PRLEARN_GITHUB_INSTALLATION_ID", "456")
    monkeypatch.setenv("PRLEARN_GITHUB_APP_PRIVATE_KEY_FILE", str(key_file))
    config = default_config(tmp_path / "prlearn.db")
    args = type("Args", (), {"fixture": None, "github_auth": "auto"})()
    assert isinstance(client_for(args, config), GitHubAppClient)


def test_private_key_normalizes_escaped_newlines() -> None:
    key_type = "PRIVATE" + " KEY"
    escaped = f"-----BEGIN {key_type}-----\\nabc\\n-----END {key_type}-----"
    assert normalize_private_key(escaped).count("\n") == 3
