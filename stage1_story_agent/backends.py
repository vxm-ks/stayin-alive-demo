"""Backend protocol and deterministic offline backend."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import time
from typing import Protocol

import httpx

from .config import Stage1Config
from .errors import BackendError, ConfigurationError


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class CompletionRequest:
    messages: list[ChatMessage]
    response_format: dict[str, str] = field(default_factory=lambda: {"type": "json_object"})


@dataclass(frozen=True)
class BackendResponse:
    content: str
    model: str
    provider: str
    request_id: str
    finish_reason: str | None = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    network_attempts: int = 1


class LLMBackend(Protocol):
    def complete(self, request: CompletionRequest) -> BackendResponse: ...


class FakeBackend:
    """Return a predefined sequence for fully offline state-machine tests."""

    def __init__(self, responses: Sequence[str | BackendResponse | Exception]) -> None:
        self._responses = list(responses)
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> BackendResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("FakeBackend response sequence exhausted")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, BackendResponse):
            return item
        return BackendResponse(
            content=item,
            model="fake-stage1",
            provider="fake",
            request_id=f"fake-{len(self.requests)}",
        )


class DeepSeekBackend:
    """Synchronous DeepSeek Chat Completions backend with bounded retries."""

    def __init__(
        self,
        config: Stage1Config,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        if config.api_key is None:
            raise ConfigurationError("DEEPSEEK_API_KEY_MISSING", "DEEPSEEK_API_KEY is required")
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=config.timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def complete(self, request: CompletionRequest) -> BackendResponse:
        payload = {
            "model": self.config.model,
            "messages": [{"role": item.role, "content": item.content} for item in request.messages],
            "response_format": request.response_format,
            "max_tokens": self.config.max_output_tokens,
            "stream": False,
            "thinking": {"type": self.config.thinking},
        }
        if self.config.thinking == "enabled":
            payload["reasoning_effort"] = self.config.reasoning_effort
        headers = {
            "Authorization": f"Bearer {self.config.api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        attempts = 0
        last_transport_error: Exception | None = None
        while attempts < self.config.max_network_attempts:
            attempts += 1
            try:
                response = self._client.post(
                    f"{self.config.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_transport_error = exc
                if attempts < self.config.max_network_attempts:
                    time.sleep(min(0.25 * 2 ** (attempts - 1), 1.0))
                    continue
                raise BackendError("DEEPSEEK_NETWORK_RETRIES_EXHAUSTED", "DeepSeek request failed after bounded network retries") from exc

            if response.status_code in {408, 429} or response.status_code >= 500:
                if attempts < self.config.max_network_attempts:
                    time.sleep(min(0.25 * 2 ** (attempts - 1), 1.0))
                    continue
                raise BackendError("DEEPSEEK_RETRYABLE_STATUS_EXHAUSTED", f"DeepSeek returned retryable HTTP status {response.status_code} after bounded retries")
            if response.status_code in {401, 403}:
                raise BackendError("DEEPSEEK_AUTH_FAILED", f"DeepSeek authentication failed with HTTP status {response.status_code}")
            if response.status_code >= 400:
                raise BackendError("DEEPSEEK_REQUEST_REJECTED", f"DeepSeek rejected the request with HTTP status {response.status_code}")
            try:
                data = response.json()
                choice = data["choices"][0]
                message = choice["message"]
                content = message.get("content") or ""
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise BackendError("DEEPSEEK_RESPONSE_INVALID", "DeepSeek returned an invalid response shape") from exc
            raw_usage = data.get("usage") or {}
            usage = {str(key): value for key, value in raw_usage.items() if isinstance(value, int)}
            return BackendResponse(
                content=content,
                model=str(data.get("model") or self.config.model),
                provider="deepseek",
                request_id=str(data.get("id") or "unavailable"),
                finish_reason=choice.get("finish_reason"),
                usage=usage,
                network_attempts=attempts,
            )
        assert last_transport_error is not None
        raise BackendError("DEEPSEEK_NETWORK_RETRIES_EXHAUSTED", "DeepSeek request failed")
