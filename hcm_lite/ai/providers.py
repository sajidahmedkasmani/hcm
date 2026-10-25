"""LLM provider abstraction (OpenAI-compatible chat completions with tool calling).

Works with any hosted API exposing `/chat/completions` + `tools`: Groq, Google Gemini
(OpenAI-compatible endpoint), OpenRouter, OpenAI, Together, Mistral, local Ollama/vLLM, etc.
The API key lives only in the backend env (AI_API_KEY); the browser never calls the provider.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def to_message(self) -> dict[str, Any]:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": json.dumps(self.arguments)}}


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: str = "unknown"
    refused: bool = False  # set by the offline engine when it declines without calling a tool


class ProviderError(RuntimeError):
    """Raised for timeouts, HTTP errors and malformed provider responses."""


class LLMProvider(Protocol):
    name: str

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        timeout: float = 15.0,
        metadata: dict[str, Any] | None = None,
    ) -> LLMResponse: ...


def new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:12]}"


class OpenAICompatibleProvider:
    def __init__(self, *, api_key: str, base_url: str, model: str, client: httpx.Client | None = None) -> None:
        self.name = f"openai-compatible:{model}"
        self._api_key = api_key
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._client = client or httpx.Client()

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        timeout: float = 15.0,
        metadata: dict[str, Any] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {"model": self._model, "messages": messages, "temperature": 0.1}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            resp = self._client.post(
                self._url, json=payload, timeout=timeout,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise ProviderError("AI provider timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"AI provider unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"AI provider returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            message = resp.json()["choices"][0]["message"]
            calls = []
            for tc in message.get("tool_calls") or []:
                raw_args = tc.get("function", {}).get("arguments") or "{}"
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                calls.append(ToolCall(id=tc.get("id") or new_call_id(), name=tc["function"]["name"], arguments=args))
            return LLMResponse(content=message.get("content"), tool_calls=calls, provider=self.name)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("Malformed response from AI provider") from exc


class FallbackProvider:
    """Tries the hosted LLM first; on any provider error, answers with the offline rules engine."""

    def __init__(self, primary: LLMProvider, fallback: LLMProvider) -> None:
        self.primary, self.fallback = primary, fallback
        self.name = f"{primary.name}+fallback"
        self.last_used = primary.name
        self.last_error: str | None = None

    def complete(self, messages, *, tools=None, json_mode=False, timeout=15.0, metadata=None) -> LLMResponse:
        try:
            resp = self.primary.complete(messages, tools=tools, json_mode=json_mode, timeout=timeout, metadata=metadata)
            self.last_used, self.last_error = self.primary.name, None
            return resp
        except ProviderError as exc:
            self.last_used, self.last_error = self.fallback.name, str(exc)
            return self.fallback.complete(messages, tools=tools, json_mode=json_mode, timeout=timeout, metadata=metadata)
