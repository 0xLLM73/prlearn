from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import urllib.error

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .util import read_json, redact, run_command


class GitHubClientError(RuntimeError):
    pass


@dataclass
class GitHubClient:
    def list_repositories(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_prs(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_commits(self, repo: str, query: dict[str, Any]) -> list[dict[str, Any]]:
        raise NotImplementedError


class FixtureGitHubClient(GitHubClient):
    def __init__(self, path: Path):
        self.path = path
        self.data = read_json(path)

    def fetch_prs(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        prs = self.data.get("prs", [])
        since = query.get("since")
        limit = query.get("limit")
        repos = set(query.get("repos") or [])
        owners = set(query.get("owners") or [])
        filtered = []
        for pr in prs:
            repo = pr.get("repo_full_name", "")
            if repos and repo not in repos:
                continue
            if owners and repo.split("/", 1)[0] not in owners:
                continue
            if since and (pr.get("updated_at") or "") < since:
                continue
            filtered.append(pr)
        filtered.sort(key=lambda item: (item.get("updated_at") or "", item.get("repo_full_name") or "", item.get("number") or 0))
        return filtered[:limit] if limit else filtered

    def list_repositories(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        repos = self.data.get("repos")
        if not repos:
            derived: dict[str, dict[str, Any]] = {}
            for pr in self.data.get("prs", []):
                full_name = str(pr.get("repo_full_name") or "")
                if not full_name or "/" not in full_name:
                    continue
                owner, name = full_name.split("/", 1)
                current = derived.setdefault(
                    full_name,
                    {
                        "full_name": full_name,
                        "owner": {"login": owner},
                        "name": name,
                        "default_branch": "main",
                        "private": False,
                        "fork": False,
                        "archived": False,
                        "language": None,
                        "size": 0,
                        "open_issues_count": 0,
                        "pushed_at": pr.get("updated_at"),
                        "updated_at": pr.get("updated_at"),
                    },
                )
                if (pr.get("updated_at") or "") > (current.get("updated_at") or ""):
                    current["updated_at"] = pr.get("updated_at")
                    current["pushed_at"] = pr.get("updated_at")
            repos = list(derived.values())
        repos = [dict(repo) for repo in repos or [] if isinstance(repo, dict)]
        requested = set(query.get("repos") or [])
        owners = set(query.get("owners") or [])
        filtered = []
        for repo in repos:
            full_name = repo_full_name(repo)
            if not full_name:
                continue
            if requested and full_name not in requested:
                continue
            if owners and full_name.split("/", 1)[0] not in owners:
                continue
            repo["full_name"] = full_name
            filtered.append(repo)
        return filtered

    def fetch_commits(self, repo: str, query: dict[str, Any]) -> list[dict[str, Any]]:
        commits_data = self.data.get("commits") or []
        if isinstance(commits_data, dict):
            commits = commits_data.get(repo) or []
        else:
            commits = [item for item in commits_data if isinstance(item, dict) and item.get("repo_full_name") == repo]
        since = query.get("since")
        author = str(query.get("author") or "").lstrip("@")
        if author == "me":
            author = ""
        limit = int(query.get("limit") or 0)
        filtered = []
        for commit in commits:
            item = normalize_repo_commit(commit)
            if since and (item.get("committed_at") or "") < since:
                continue
            login = str(item.get("author_login") or "")
            if author and login and login != author:
                continue
            filtered.append(item)
        filtered.sort(key=lambda item: item.get("committed_at") or "", reverse=True)
        return filtered[:limit] if limit else filtered


@dataclass
class GitHubAppClient(GitHubClient):
    app_id: str
    installation_id: str
    private_key_pem: str
    api_url: str = "https://api.github.com"
    default_author: str | None = None
    request_timeout: int = 30

    def __post_init__(self) -> None:
        self.api_url = self.api_url.rstrip("/")
        self._token: str | None = None
        self._token_expires_at: datetime | None = None

    def fetch_prs(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        repos = query.get("repos") or []
        owners = set(query.get("owners") or [])
        author = self._resolve_author(query.get("author"))
        since = query.get("since")
        limit = int(query.get("limit") or 0)
        candidates: list[dict[str, Any]] = []
        for repo in self.installation_repositories():
            full_name = str(repo.get("full_name") or repo.get("nameWithOwner") or "")
            if not full_name:
                continue
            if repos and full_name not in repos:
                continue
            if owners and full_name.split("/", 1)[0] not in owners:
                continue
            for pull in self._pulls_for_repo(full_name, since=since, author=author):
                candidates.append({"repo_full_name": full_name, "number": int(pull["number"]), "updated_at": pull.get("updated_at") or ""})
        candidates.sort(key=lambda item: (item.get("updated_at") or "", item["repo_full_name"], item["number"]), reverse=True)
        selected = candidates[:limit] if limit else candidates
        return [self._pr_view(item["repo_full_name"], int(item["number"])) for item in selected]

    def list_repositories(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        requested = set(query.get("repos") or [])
        owners = set(query.get("owners") or [])
        repos = []
        for repo in self.installation_repositories():
            full_name = repo_full_name(repo)
            if not full_name:
                continue
            if requested and full_name not in requested:
                continue
            if owners and full_name.split("/", 1)[0] not in owners:
                continue
            normalized = dict(repo)
            normalized["full_name"] = full_name
            repos.append(normalized)
        return repos

    def fetch_commits(self, repo: str, query: dict[str, Any]) -> list[dict[str, Any]]:
        params: dict[str, str] = {"per_page": "100"}
        author = self._resolve_author(query.get("author"))
        if author:
            params["author"] = author
        if query.get("since"):
            params["since"] = str(query["since"])
        if query.get("until"):
            params["until"] = str(query["until"])
        if query.get("sha"):
            params["sha"] = str(query["sha"])
        limit = int(query.get("limit") or 0)
        commits = [
            normalize_repo_commit(item)
            for item in self._paginate(f"/repos/{repo_path(repo)}/commits", token=self.installation_token(), params=params)
        ]
        commits = [item for item in commits if item.get("sha")]
        return commits[:limit] if limit else commits

    def installation_repositories(self) -> list[dict[str, Any]]:
        token = self.installation_token()
        data = self._paginate("/installation/repositories", root_key="repositories", token=token)
        return data

    def installation_token(self) -> str:
        now = datetime.now(UTC)
        if self._token and self._token_expires_at and self._token_expires_at > now + timedelta(seconds=60):
            return self._token
        data = self._request_json(
            "POST",
            f"/app/installations/{self.installation_id}/access_tokens",
            token=self._app_jwt(),
        )
        token = str(data.get("token") or "")
        if not token:
            raise GitHubClientError("GitHub App installation token response did not include a token")
        self._token = token
        expires_at = str(data.get("expires_at") or "")
        try:
            self._token_expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            self._token_expires_at = now + timedelta(minutes=50)
        return token

    def _resolve_author(self, author: str | None) -> str | None:
        if not author:
            return None
        if author == "@me":
            if not self.default_author:
                raise GitHubClientError("GitHub App auth cannot resolve @me; set PRLEARN_GITHUB_AUTHOR or github.default_author")
            return self.default_author.lstrip("@")
        return author.lstrip("@")

    def _pulls_for_repo(self, repo: str, *, since: str | None, author: str | None) -> list[dict[str, Any]]:
        pulls: list[dict[str, Any]] = []
        for pull in self._paginate(
            f"/repos/{repo_path(repo)}/pulls",
            token=self.installation_token(),
            params={"state": "all", "sort": "updated", "direction": "desc", "per_page": "100"},
        ):
            updated_at = str(pull.get("updated_at") or "")
            if since and updated_at < since:
                break
            login = ((pull.get("user") or {}).get("login") if isinstance(pull.get("user"), dict) else None) or ""
            if author and login != author:
                continue
            pulls.append(pull)
        return pulls

    def _pr_view(self, repo: str, number: int) -> dict[str, Any]:
        token = self.installation_token()
        pr = self._request_json("GET", f"/repos/{repo_path(repo)}/pulls/{number}", token=token)
        issue = self._request_json_optional("GET", f"/repos/{repo_path(repo)}/issues/{number}", token=token, default={})
        head_sha = ((pr.get("head") or {}).get("sha") if isinstance(pr.get("head"), dict) else None) or pr.get("headRefOid")
        return {
            "repo_full_name": repo,
            "number": pr.get("number"),
            "github_id": pr.get("node_id") or pr.get("id"),
            "title": pr.get("title"),
            "body": pr.get("body"),
            "url": pr.get("html_url") or pr.get("url"),
            "author_login": ((pr.get("user") or {}).get("login") if isinstance(pr.get("user"), dict) else None),
            "state": str(pr.get("state") or "").upper(),
            "is_merged": bool(pr.get("merged_at")),
            "created_at": pr.get("created_at"),
            "updated_at": pr.get("updated_at"),
            "closed_at": pr.get("closed_at"),
            "merged_at": pr.get("merged_at"),
            "head_sha": head_sha,
            "base_ref": ((pr.get("base") or {}).get("ref") if isinstance(pr.get("base"), dict) else None),
            "head_ref": ((pr.get("head") or {}).get("ref") if isinstance(pr.get("head"), dict) else None),
            "labels": (issue.get("labels") if isinstance(issue, dict) else None) or pr.get("labels") or [],
            "review_decision": None,
            "mergeable": pr.get("mergeable"),
            "reviews": [normalize_review(item) for item in self._paginate(f"/repos/{repo_path(repo)}/pulls/{number}/reviews", token=token)],
            "issue_comments": [normalize_issue_comment(item) for item in self._paginate(f"/repos/{repo_path(repo)}/issues/{number}/comments", token=token)],
            "review_comments": [normalize_review_comment(item) for item in self._paginate(f"/repos/{repo_path(repo)}/pulls/{number}/comments", token=token)],
            "timeline": self._request_json_optional("GET", f"/repos/{repo_path(repo)}/issues/{number}/timeline", token=token, default=[]),
            "files": [normalize_file(item) for item in self._paginate(f"/repos/{repo_path(repo)}/pulls/{number}/files", token=token)],
            "commits": [normalize_commit(item) for item in self._paginate(f"/repos/{repo_path(repo)}/pulls/{number}/commits", token=token)],
            "check_runs": self._check_runs(repo, str(head_sha or "")) if head_sha else [],
        }

    def _check_runs(self, repo: str, head_sha: str) -> list[dict[str, Any]]:
        data = self._request_json_optional("GET", f"/repos/{repo_path(repo)}/commits/{quote(head_sha, safe='')}/check-runs", token=self.installation_token(), default={})
        runs = data.get("check_runs", []) if isinstance(data, dict) else []
        normalized = []
        for run in runs:
            item = dict(run)
            item["url"] = item.get("html_url") or item.get("details_url") or item.get("url")
            conclusion = item.get("conclusion") or item.get("status")
            item["conclusion"] = conclusion
            if conclusion and str(conclusion).lower() not in {"success", "skipped", "cancelled"} and item.get("id"):
                item["annotations"] = self._request_json_optional(
                    "GET",
                    f"/repos/{repo_path(repo)}/check-runs/{item['id']}/annotations",
                    token=self.installation_token(),
                    default=[],
                )
            normalized.append(item)
        return normalized

    def _paginate(self, path: str, *, token: str, root_key: str | None = None, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        all_items: list[dict[str, Any]] = []
        page = 1
        per_page = int((params or {}).get("per_page") or 100)
        while True:
            page_params = {**(params or {}), "page": str(page), "per_page": str(per_page)}
            data = self._request_json("GET", path, token=token, params=page_params)
            items = data.get(root_key, []) if root_key and isinstance(data, dict) else data
            if not isinstance(items, list):
                return all_items
            all_items.extend(item for item in items if isinstance(item, dict))
            if len(items) < per_page:
                break
            page += 1
        return all_items

    def _request_json_optional(self, method: str, path: str, *, token: str, default: Any, params: dict[str, str] | None = None) -> Any:
        try:
            return self._request_json(method, path, token=token, params=params)
        except GitHubClientError:
            return default

    def _request_json(self, method: str, path: str, *, token: str, payload: dict[str, Any] | None = None, params: dict[str, str] | None = None) -> Any:
        try:
            return self._request_json_once(method, path, token=token, payload=payload, params=params)
        except GitHubClientError as exc:
            if "GitHub API 401" not in str(exc) or path.startswith("/app/"):
                raise
            self._token = None
            self._token_expires_at = None
            return self._request_json_once(method, path, token=self.installation_token(), payload=payload, params=params)

    def _request_json_once(self, method: str, path: str, *, token: str, payload: dict[str, Any] | None = None, params: dict[str, str] | None = None) -> Any:
        url = self.api_url + path
        if params:
            url += "?" + urlencode(params)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "prlearn",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=self.request_timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise GitHubClientError(redact(f"GitHub API {exc.code} for {method} {path}: {detail}")) from exc
        except OSError as exc:
            raise GitHubClientError(redact(f"GitHub API request failed for {method} {path}: {exc}")) from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GitHubClientError(f"failed to parse GitHub API JSON for {method} {path}: {exc}") from exc

    def _app_jwt(self) -> str:
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        payload = {"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)}
        signing_input = b".".join([b64url(json.dumps(header, separators=(",", ":")).encode()), b64url(json.dumps(payload, separators=(",", ":")).encode())])
        private_key = serialization.load_pem_private_key(self.private_key_pem.encode("utf-8"), password=None)
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return b".".join([signing_input, b64url(signature)]).decode("ascii")


class GhCliClient(GitHubClient):
    def list_repositories(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        limit = str(query.get("repo_limit") or 1000)
        fields = "nameWithOwner,name,owner,defaultBranchRef,isPrivate,isFork,isArchived,primaryLanguage,pushedAt,updatedAt"
        owners = list(query.get("owners") or [])
        repos: list[dict[str, Any]] = []
        if owners:
            for owner in owners:
                repos.extend(self._gh_json(["repo", "list", str(owner), "--limit", limit, "--json", fields]) or [])
        else:
            repos = self._gh_json(["repo", "list", "--limit", limit, "--json", fields])
        requested = set(query.get("repos") or [])
        owner_filter = set(owners)
        normalized = []
        for repo in repos or []:
            full_name = repo_full_name(repo)
            if not full_name:
                continue
            if requested and full_name not in requested:
                continue
            if owner_filter and full_name.split("/", 1)[0] not in owner_filter:
                continue
            item = dict(repo)
            item["full_name"] = full_name
            normalized.append(item)
        return normalized

    def fetch_prs(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        repos = query.get("repos") or []
        owners = query.get("owners") or []
        all_prs: list[dict[str, Any]] = []
        for item in self._search_prs(query):
            repo_info = item.get("repository") or {}
            repo = repo_info.get("fullName") or repo_info.get("nameWithOwner") or item.get("repo_full_name")
            if repo:
                all_prs.append(self._pr_view(repo, int(item["number"])))
        return all_prs

    def fetch_commits(self, repo: str, query: dict[str, Any]) -> list[dict[str, Any]]:
        args = ["api", f"repos/{repo}/commits", "--paginate"]
        author = self._resolve_author(query.get("author"))
        if author:
            args.extend(["-F", f"author={author}"])
        if query.get("since"):
            args.extend(["-F", f"since={query['since']}"])
        if query.get("until"):
            args.extend(["-F", f"until={query['until']}"])
        if query.get("sha"):
            args.extend(["-F", f"sha={query['sha']}"])
        limit = int(query.get("limit") or 0)
        commits = [normalize_repo_commit(item) for item in self._gh_json(args) or []]
        commits = [item for item in commits if item.get("sha")]
        return commits[:limit] if limit else commits

    def _gh_json(self, args: list[str]) -> Any:
        result = run_command(["gh", *args], timeout=60)
        if result.returncode != 0:
            raise GitHubClientError(redact(result.stderr.strip() or result.stdout.strip() or "gh command failed"))
        try:
            return json.loads(result.stdout or "null")
        except json.JSONDecodeError as exc:
            raise GitHubClientError(f"failed to parse gh JSON: {exc}") from exc

    def _gh_json_optional(self, args: list[str], default: Any) -> Any:
        try:
            return self._gh_json(args)
        except GitHubClientError:
            return default

    def _search_prs(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        args = ["search", "prs", "--limit", str(query.get("limit") or 100), "--json", "number,repository,updatedAt"]
        author = self._resolve_author(query.get("author"))
        if author:
            args.extend(["--author", author])
        for repo in query.get("repos") or []:
            args.extend(["--repo", repo])
        for owner in query.get("owners") or []:
            args.extend(["--owner", owner])
        if query.get("since"):
            args.extend(["--updated", f">={query['since']}"])
        return self._gh_json(args)

    def _resolve_author(self, author: str | None) -> str | None:
        if not author:
            return None
        if author == "@me":
            user = self._gh_json(["api", "user"])
            return user.get("login")
        return author.lstrip("@")

    def _pr_view(self, repo: str, number: int) -> dict[str, Any]:
        fields = "id,number,title,url,body,author,state,isDraft,createdAt,updatedAt,closedAt,mergedAt,headRefName,baseRefName,headRefOid,labels,reviewDecision,mergeable,comments,reviews,commits"
        pr = self._gh_json(["pr", "view", str(number), "--repo", repo, "--json", fields])
        head_sha = pr.get("headRefOid")
        checks = self._check_runs(repo, head_sha) if head_sha else []
        review_comments = self._gh_json(["api", f"repos/{repo}/pulls/{number}/comments", "--paginate"])
        issue_comments = self._gh_json(["api", f"repos/{repo}/issues/{number}/comments", "--paginate"])
        timeline = self._gh_json_optional(["api", f"repos/{repo}/issues/{number}/timeline", "--paginate"], [])
        files = self._gh_json_optional(["api", f"repos/{repo}/pulls/{number}/files", "--paginate"], [])
        return {
            "repo_full_name": repo,
            "number": pr.get("number"),
            "github_id": pr.get("id"),
            "title": pr.get("title"),
            "body": pr.get("body"),
            "url": pr.get("url"),
            "author_login": (pr.get("author") or {}).get("login"),
            "state": pr.get("state"),
            "is_merged": bool(pr.get("mergedAt")),
            "created_at": pr.get("createdAt"),
            "updated_at": pr.get("updatedAt"),
            "closed_at": pr.get("closedAt"),
            "merged_at": pr.get("mergedAt"),
            "head_sha": head_sha,
            "base_ref": pr.get("baseRefName"),
            "head_ref": pr.get("headRefName"),
            "labels": pr.get("labels") or [],
            "review_decision": pr.get("reviewDecision"),
            "mergeable": pr.get("mergeable"),
            "reviews": pr.get("reviews") or [],
            "issue_comments": issue_comments or pr.get("comments") or [],
            "review_comments": review_comments or [],
            "timeline": timeline or [],
            "files": files or [],
            "commits": pr.get("commits") or [],
            "check_runs": checks or [],
        }

    def _check_runs(self, repo: str, head_sha: str) -> list[dict[str, Any]]:
        data = self._gh_json_optional(["api", f"repos/{repo}/commits/{head_sha}/check-runs", "--paginate"], {})
        runs = data.get("check_runs", []) if isinstance(data, dict) else []
        for run in runs:
            conclusion = run.get("conclusion") or run.get("status")
            run["conclusion"] = conclusion
            if conclusion and str(conclusion).lower() not in {"success", "skipped", "cancelled"} and run.get("id"):
                run["annotations"] = self._gh_json_optional(
                    ["api", f"repos/{repo}/check-runs/{run['id']}/annotations", "--paginate"],
                    [],
                )
        return runs


def b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def repo_path(repo: str) -> str:
    owner, name = repo.split("/", 1)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


def normalize_user(item: dict[str, Any]) -> dict[str, str | None]:
    user = item.get("user") or item.get("author") or {}
    return {"login": user.get("login") if isinstance(user, dict) else None}


def normalize_review(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("node_id") or item.get("id"),
        "author": normalize_user(item),
        "state": item.get("state"),
        "body": item.get("body"),
        "url": item.get("html_url") or item.get("url"),
        "created_at": item.get("submitted_at") or item.get("created_at"),
        "updated_at": item.get("updated_at") or item.get("submitted_at") or item.get("created_at"),
    }


def normalize_review_comment(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("node_id") or item.get("id"),
        "author": normalize_user(item),
        "body": item.get("body"),
        "path": item.get("path"),
        "line": item.get("line") or item.get("original_line"),
        "url": item.get("html_url") or item.get("url"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "commit_sha": item.get("commit_id") or item.get("original_commit_id"),
    }


def normalize_issue_comment(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("node_id") or item.get("id"),
        "author": normalize_user(item),
        "body": item.get("body"),
        "url": item.get("html_url") or item.get("url"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def normalize_file(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    normalized["path"] = item.get("filename") or item.get("path")
    return normalized


def normalize_commit(item: dict[str, Any]) -> dict[str, Any]:
    commit = item.get("commit") or {}
    return {
        "id": item.get("node_id") or item.get("sha"),
        "oid": item.get("sha"),
        "message": commit.get("message") if isinstance(commit, dict) else item.get("message"),
        "url": item.get("html_url") or item.get("url"),
    }


def normalize_repo_commit(item: dict[str, Any]) -> dict[str, Any]:
    commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    commit_author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
    return {
        "repo_full_name": item.get("repo_full_name"),
        "sha": item.get("sha") or item.get("oid") or item.get("id"),
        "message": commit.get("message") or item.get("message") or "",
        "author_login": author.get("login") or item.get("author_login") or commit_author.get("name"),
        "committed_at": commit_author.get("date") or item.get("committed_at") or item.get("committedDate"),
        "url": item.get("html_url") or item.get("url"),
        "raw": item,
    }


def repo_full_name(repo: dict[str, Any]) -> str:
    full_name = str(repo.get("full_name") or repo.get("nameWithOwner") or "")
    if full_name:
        return full_name
    owner = repo.get("owner")
    owner_login = owner.get("login") if isinstance(owner, dict) else owner
    name = repo.get("name")
    if owner_login and name:
        return f"{owner_login}/{name}"
    return ""


def github_app_client_from_config(config: dict[str, Any]) -> GitHubAppClient:
    app_id = env_or_config(config, "app_id", "app_id_env")
    installation_id = env_or_config(config, "installation_id", "installation_id_env")
    private_key = private_key_from_config(config)
    if not app_id or not installation_id or not private_key:
        raise GitHubClientError(
            "GitHub App auth is not configured; set PRLEARN_GITHUB_APP_ID, PRLEARN_GITHUB_INSTALLATION_ID, and PRLEARN_GITHUB_APP_PRIVATE_KEY_FILE"
        )
    default_author = env_or_config(config, "default_author", "default_author_env")
    return GitHubAppClient(
        app_id=app_id,
        installation_id=installation_id,
        private_key_pem=private_key,
        api_url=str(config.get("api_url") or "https://api.github.com"),
        default_author=default_author or None,
    )


def github_app_configured(config: dict[str, Any]) -> bool:
    try:
        app_id = env_or_config(config, "app_id", "app_id_env")
        installation_id = env_or_config(config, "installation_id", "installation_id_env")
        private_key = private_key_from_config(config)
    except OSError:
        return False
    return bool(app_id and installation_id and private_key)


def env_or_config(config: dict[str, Any], value_key: str, env_key: str) -> str:
    env_name = str(config.get(env_key) or "")
    if env_name and os.environ.get(env_name):
        return str(os.environ[env_name]).strip()
    return str(config.get(value_key) or "").strip()


def private_key_from_config(config: dict[str, Any]) -> str:
    env_name = str(config.get("private_key_env") or "PRLEARN_GITHUB_APP_PRIVATE_KEY")
    if env_name and os.environ.get(env_name):
        return normalize_private_key(os.environ[env_name])
    file_env_name = str(config.get("private_key_file_env") or "PRLEARN_GITHUB_APP_PRIVATE_KEY_FILE")
    file_path = os.environ.get(file_env_name) if file_env_name else None
    file_path = file_path or str(config.get("private_key_file") or "").strip()
    if file_path:
        return normalize_private_key(Path(file_path).expanduser().read_text())
    return ""


def normalize_private_key(value: str) -> str:
    text = value.strip().replace("\\n", "\n")
    if text and not text.endswith("\n"):
        text += "\n"
    return text
