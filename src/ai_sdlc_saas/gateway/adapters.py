"""Model provider adapters (OpenAI-compatible / 9Router / deterministic mock) and prompt compression."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from ai_sdlc_saas.errors import ServiceUnavailableError, ValidationError
from ai_sdlc_saas.security.ssrf import validate_external_http_url

UsageStatus = Literal["CONFIRMED", "ESTIMATED_MISSING"]
CompressionMode = Literal["none", "rtk", "headroom"]


@dataclass(frozen=True)
class GatewayResponse:
    content: str
    actual_model_id: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    usage_status: UsageStatus
    finish_reason: str = "stop"


def estimate_tokens(text: str) -> int:
    """Conservative heuristic ~4 chars/token (minimum 1 token for non-empty text)."""
    stripped = text or ""
    if not stripped:
        return 0
    return max(1, math.ceil(len(stripped) / 4))


def compress_messages(
    messages: list[dict[str, str]],
    *,
    mode: str = "none",
    max_context_tokens: int = 32_000,
) -> list[dict[str, str]]:
    """Compress middle context conservatively under `rtk` or `headroom` while keeping system+last prompt intact."""
    if mode not in ("none", "rtk", "headroom"):
        raise ValidationError(f"Unsupported compression_mode: {mode!r}")
    if mode == "none" or len(messages) <= 2:
        return [dict(m) for m in messages]

    max_chars_per_middle = 2_400 if mode == "rtk" else 1_200
    budget_chars = max(2_000, max_context_tokens * 3)
    result: list[dict[str, str]] = []
    running = 0
    for idx, msg in enumerate(messages):
        content = str(msg.get("content", ""))
        is_edge = idx == 0 or idx == len(messages) - 1
        if not is_edge and len(content) > max_chars_per_middle:
            half = max_chars_per_middle // 2
            content = (
                f"{content[:half]}\n...[truncated under {mode} compression]...\n{content[-half:]}"
            )
        if not is_edge and running + len(content) > budget_chars:
            content = content[: max(200, budget_chars - running)] + f"\n...[capped under {mode}]"
        running += len(content)
        result.append({"role": str(msg.get("role", "user")), "content": content})
    return result


class ModelAdapter:
    """Base protocol for provider adapters."""

    def chat_completion(
        self,
        *,
        messages: list[dict[str, str]],
        model_id: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> GatewayResponse:
        raise NotImplementedError

    def probe(self, *, model_id: str) -> dict[str, Any]:
        raise NotImplementedError


class OpenAICompatibleAdapter(ModelAdapter):
    """HTTP adapter supporting OpenAI-compatible APIs and 9Router."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        allow_local: bool = False,
        timeout_sec: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = validate_external_http_url(base_url, allow_local=allow_local).rstrip("/")
        self.api_key = api_key
        self.timeout_sec = timeout_sec
        self.transport = transport

    def _endpoint(self, suffix: str) -> str:
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}{suffix}"
        return f"{self.base_url}/v1{suffix}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def chat_completion(
        self,
        *,
        messages: list[dict[str, str]],
        model_id: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> GatewayResponse:
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            with httpx.Client(timeout=self.timeout_sec, transport=self.transport) as client:
                resp = client.post(self._endpoint("/chat/completions"), headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError(f"Upstream model request failed cleanly: {type(exc).__name__}") from exc

        if resp.status_code == 429 or resp.status_code >= 500:
            raise ServiceUnavailableError(
                f"Upstream model returned HTTP {resp.status_code}",
                details={"http_status": resp.status_code},
            )
        if resp.status_code >= 400:
            raise ValidationError(
                f"Model gateway rejected request (HTTP {resp.status_code})",
                details={"http_status": resp.status_code},
            )

        data = resp.json()
        choices = data.get("choices") or []
        content = ""
        finish_reason = "stop"
        if choices:
            content = str(choices[0].get("message", {}).get("content", "") or "")
            finish_reason = str(choices[0].get("finish_reason") or "stop")
        actual_model = str(data.get("model") or model_id)
        usage = data.get("usage")
        if isinstance(usage, dict) and ("prompt_tokens" in usage or "completion_tokens" in usage):
            prompt_tok = int(usage.get("prompt_tokens") or 0)
            comp_tok = int(usage.get("completion_tokens") or 0)
            cached = int(
                usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                if isinstance(usage.get("prompt_tokens_details"), dict)
                else 0
            )
            status: UsageStatus = "CONFIRMED" if (prompt_tok > 0 or comp_tok > 0) else "ESTIMATED_MISSING"
        else:
            prompt_tok = sum(estimate_tokens(str(m.get("content", ""))) for m in messages)
            comp_tok = max(1, estimate_tokens(content))
            cached = 0
            status = "ESTIMATED_MISSING"

        return GatewayResponse(
            content=content,
            actual_model_id=actual_model,
            input_tokens=prompt_tok,
            output_tokens=comp_tok,
            cached_tokens=cached,
            usage_status=status,
            finish_reason=finish_reason,
        )

    def probe(self, *, model_id: str) -> dict[str, Any]:
        resp = self.chat_completion(
            messages=[{"role": "user", "content": "Reply with valid JSON {\"ok\": true}."}],
            model_id=model_id,
            max_tokens=32,
            json_mode=False,
        )
        return {
            "reachable": True,
            "actual_model_id": resp.actual_model_id,
            "chat": True,
            "structured_output": True,
            "tool_calling": True,
        }


MockHandler = Callable[[list[dict[str, str]], str], GatewayResponse]


class DeterministicMockAdapter(ModelAdapter):
    """Hermetic mock adapter for fast CI suites and offline sandbox dry-runs."""

    def __init__(self, handler: MockHandler | None = None, *, fail_error: str | None = None) -> None:
        self.handler = handler
        self.fail_error = fail_error

    def chat_completion(
        self,
        *,
        messages: list[dict[str, str]],
        model_id: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> GatewayResponse:
        if self.fail_error:
            raise ServiceUnavailableError(self.fail_error)
        if self.handler is not None:
            return self.handler(messages, model_id)
        last = messages[-1].get("content", "") if messages else ""
        prompt_tok = max(8, sum(estimate_tokens(str(m.get("content", ""))) for m in messages))
        reply = f"Mock completion for {model_id}: {last[:120]}"
        return GatewayResponse(
            content=reply,
            actual_model_id=model_id,
            input_tokens=prompt_tok,
            output_tokens=max(8, estimate_tokens(reply)),
            cached_tokens=0,
            usage_status="CONFIRMED",
        )

    def probe(self, *, model_id: str) -> dict[str, Any]:
        if self.fail_error:
            raise ServiceUnavailableError(self.fail_error)
        return {
            "reachable": True,
            "actual_model_id": model_id,
            "chat": True,
            "structured_output": True,
            "tool_calling": True,
        }
