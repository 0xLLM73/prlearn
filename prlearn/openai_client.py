from __future__ import annotations

import json
import os
import urllib.error
from dataclasses import dataclass
from typing import Any
from urllib.request import Request, urlopen

from .redaction import redact_text


class OpenAIError(RuntimeError):
    code = "openai_error"


class OpenAIAuthError(OpenAIError):
    code = "auth"


class OpenAIUnavailable(OpenAIError):
    code = "unavailable"


class OpenAITimeout(OpenAIError):
    code = "timeout"


@dataclass(frozen=True)
class OpenAIResponsesClient:
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    timeout: float = 180
    model: str = "gpt-5.5"
    reasoning_effort: str | None = "low"
    max_output_tokens: int | None = 2048

    @property
    def model_label(self) -> str:
        return self.model

    def version(self) -> dict[str, Any]:
        if not self.api_key:
            raise OpenAIAuthError("OpenAI API key is not configured; set OPENAI_API_KEY or openai.api_key_env")
        return {"provider": "openai", "base_url": self.base_url.rstrip("/")}

    def chat(self, *, model: str, messages: list[dict[str, str]], options: dict[str, Any], schema: dict[str, Any] | None = None) -> dict[str, Any]:
        self.version()
        payload: dict[str, Any] = {
            "model": model or self.model,
            "input": response_input(messages),
        }
        reasoning_effort = str(options.get("reasoning_effort") or self.reasoning_effort or "").strip()
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}
        max_output_tokens = options.get("max_output_tokens", self.max_output_tokens)
        if max_output_tokens:
            payload["max_output_tokens"] = int(max_output_tokens)
        if schema is not None:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "prlearn_extraction",
                    "strict": True,
                    "schema": schema,
                }
            }
        data = self._request_json("POST", "/responses", payload)
        return {
            "message": {"content": response_text(data)},
            "model": data.get("model") or model or self.model,
            "provider": "openai-responses",
            "id": data.get("id"),
            "usage": data.get("usage"),
        }

    def _request_json(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            self.base_url.rstrip("/") + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "prlearn",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except TimeoutError as exc:
            raise OpenAITimeout(f"OpenAI request timed out after {self.timeout:g}s") from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            error_cls = OpenAIAuthError if exc.code in {401, 403} else OpenAIUnavailable
            raise error_cls(redact_text(f"OpenAI API {exc.code}: {detail}").text) from exc
        except OSError as exc:
            raise OpenAIUnavailable(redact_text(f"OpenAI request failed: {exc}").text) from exc
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise OpenAIUnavailable(f"failed to parse OpenAI API JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise OpenAIUnavailable("OpenAI API returned non-object JSON")
        return data


def response_input(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        if role == "system":
            role = "developer"
        items.append({"role": role, "content": str(message.get("content") or "")})
    return items


def response_text(data: dict[str, Any]) -> str:
    if data.get("output_text"):
        return str(data["output_text"])
    parts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                parts.append(str(content.get("text") or ""))
    return "\n".join(part for part in parts if part)


def client_from_config(config: dict[str, Any]) -> OpenAIResponsesClient:
    env_name = str(config.get("api_key_env") or "OPENAI_API_KEY")
    api_key = os.environ.get(env_name, "")
    return OpenAIResponsesClient(
        api_key=api_key,
        base_url=str(config.get("base_url") or "https://api.openai.com/v1"),
        timeout=float(config.get("request_timeout_seconds") or 180),
        model=str(config.get("model") or "gpt-5.5"),
        reasoning_effort=str(config.get("reasoning_effort") or "low"),
        max_output_tokens=int(config.get("max_output_tokens") or 2048),
    )
