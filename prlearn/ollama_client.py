from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class OllamaError(RuntimeError):
    code = "ollama_error"


class OllamaUnavailable(OllamaError):
    code = "unavailable"


class OllamaRemoteHostBlocked(OllamaError):
    code = "remote_host_blocked"


class OllamaModelMissing(OllamaError):
    code = "model_missing"


class OllamaInvalidJSON(OllamaError):
    code = "invalid_json"


class OllamaTimeout(OllamaError):
    code = "timeout"


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class OllamaClient:
    base_url: str = "http://127.0.0.1:11434"
    timeout: float = 15
    require_localhost: bool = True

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        host = parsed.hostname or ""
        if self.require_localhost and host not in LOCAL_HOSTS:
            raise OllamaRemoteHostBlocked(f"Ollama base URL must be localhost, got {self.base_url}")

    def version(self) -> dict[str, Any]:
        return self._request("GET", "/api/version")

    def tags(self) -> dict[str, Any]:
        return self._request("GET", "/api/tags")

    def chat(self, *, model: str, messages: list[dict[str, str]], options: dict[str, Any], schema: dict[str, Any] | None = None) -> dict[str, Any]:
        request_options = {key: value for key, value in options.items() if key not in {"stream", "think"}}
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "options": request_options,
            "stream": bool(options.get("stream", False)),
        }
        if "think" in options:
            payload["think"] = bool(options["think"])
        if schema is not None:
            payload["format"] = schema
        return self._request("POST", "/api/chat", payload)

    def embed(self, *, model: str, input: str | list[str], options: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "input": input}
        if options:
            payload["options"] = options
        return self._request("POST", "/api/embed", payload)

    def installed_models(self) -> set[str]:
        data = self.tags()
        models = data.get("models") if isinstance(data, dict) else []
        names = set()
        for item in models or []:
            if isinstance(item, dict) and item.get("name"):
                names.add(str(item["name"]))
        return names

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.base_url.rstrip("/") + path
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except TimeoutError as exc:
            raise OllamaTimeout(f"Ollama request timed out: {path}") from exc
        except urllib.error.HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            if exc.code == 404 and "model" in message.lower():
                raise OllamaModelMissing(message or f"model missing for {path}") from exc
            raise OllamaUnavailable(f"Ollama HTTP {exc.code}: {message}") from exc
        except OSError as exc:
            raise OllamaUnavailable(f"Ollama unavailable at {self.base_url}: {exc}") from exc
        try:
            data = json.loads(body or "{}")
        except json.JSONDecodeError as exc:
            raise OllamaInvalidJSON(f"Ollama returned invalid JSON for {path}") from exc
        if not isinstance(data, dict):
            raise OllamaInvalidJSON(f"Ollama returned non-object JSON for {path}")
        return data


def client_from_config(config: dict[str, Any], *, timeout: float = 15) -> OllamaClient:
    return OllamaClient(
        base_url=str(config.get("base_url") or "http://127.0.0.1:11434"),
        timeout=timeout,
        require_localhost=bool(config.get("require_localhost", True)),
    )
