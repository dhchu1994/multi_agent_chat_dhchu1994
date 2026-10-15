"""
Core LLM wrapper shared by every agent.

* ``OpenAICompatClient`` talks to any OpenAI-compatible chat-completions endpoint with
  streaming (so first-token latency can be logged) and a pinned model.
* ``MockClient`` is an offline, deterministic stand-in that follows the behaviour rules, so the
  whole portal, both harnesses and the tests run without a network or an API key
  (``llm.provider: mock``).
* ``call_with_retry`` implements the failure rule: one automatic retry, then raise
  ``AgentCallFailed`` so the caller can show a neutral line and keep the page and timer alive.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config_loader import (
    PortalConfig,
    canonical_provider,
    get_provider_env_vars,
    resolve_api_key,
)


@dataclass
class LLMResult:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    first_token_ms: int = 0
    total_ms: int = 0


class AgentCallFailed(RuntimeError):
    def __init__(self, agent: str, error_type: str, retries: int) -> None:
        super().__init__(f"{agent} call failed: {error_type} after {retries} retries")
        self.agent, self.error_type, self.retries = agent, error_type, retries


class LLMClient:
    """Interface: ``complete`` returns the full text of one model call."""

    model_version: str = ""

    def complete(self, system: str, user: str, *, temperature: float, max_tokens: int,
                 json_mode: bool = True, meta: dict[str, Any] | None = None) -> LLMResult:
        raise NotImplementedError


class OpenAICompatClient(LLMClient):
    """Client for OpenAI and OpenAI-compatible providers (ChatGPT, DeepSeek, GLM, Qwen)."""

    def __init__(self, cfg: PortalConfig) -> None:
        from openai import OpenAI

        self.cfg = cfg
        self.provider = canonical_provider(cfg.llm.provider)
        key, var_used = resolve_api_key(cfg)
        if not key:
            expected = get_provider_env_vars(self.provider, cfg.llm.api_key_env)
            raise RuntimeError(
                f"llm.provider is '{cfg.llm.provider}' but no API key was found in environment. "
                f"Checked environment variables: {', '.join(expected)}. "
                f"Set one (e.g. {expected[0]}=your-key), or use llm.provider: mock "
                "(PORTAL_LLM_PROVIDER=mock) for offline testing."
            )
        import httpx
        base_url = cfg.llm.base_url or None
        max_p = getattr(cfg, "max_concurrent_participants", 100) or 100
        http_client = httpx.Client(
            timeout=cfg.llm.timeout_seconds,
            limits=httpx.Limits(
                max_connections=max(100, max_p * 3),
                max_keepalive_connections=max(50, max_p),
            ),
        )
        self._client = OpenAI(base_url=base_url, api_key=key, timeout=cfg.llm.timeout_seconds, http_client=http_client)
        self.model_version = cfg.llm.model

    def complete(self, system: str, user: str, *, temperature: float, max_tokens: int,
                 json_mode: bool = True, meta: dict[str, Any] | None = None) -> LLMResult:
        import openai
        t0 = time.monotonic()
        kwargs: dict[str, Any] = dict(
            model=self.model_version, stream=True,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        if getattr(self.cfg.llm, "send_temperature", True):
            kwargs["temperature"] = temperature
        token_param = getattr(self.cfg.llm, "token_param", "max_tokens")
        if token_param == "max_completion_tokens":
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens

        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if getattr(self.cfg.llm, "supports_stream_usage", True):
            kwargs["stream_options"] = {"include_usage": True}

        def make_stream(call_kwargs: dict[str, Any]):
            return self._client.chat.completions.create(**call_kwargs)

        try:
            stream = make_stream(kwargs)
        except (TypeError, openai.BadRequestError, openai.APIError) as exc:
            err_msg = str(exc).lower()
            if "stream_options" in kwargs and ("stream_options" in err_msg or isinstance(exc, TypeError)):
                kwargs.pop("stream_options", None)
            if "response_format" in kwargs and "response_format" in err_msg:
                kwargs.pop("response_format", None)
            if "temperature" in kwargs and "temperature" in err_msg:
                kwargs.pop("temperature", None)
            if "max_tokens" in kwargs and ("max_tokens" in err_msg or "max_completion_tokens" in err_msg):
                kwargs.pop("max_tokens", None)
                kwargs["max_completion_tokens"] = max_tokens
            stream = make_stream(kwargs)

        parts: list[str] = []
        first_ms = 0
        t_in = t_out = 0
        try:
            for chunk in stream:
                if getattr(chunk, "usage", None):
                    t_in, t_out = chunk.usage.prompt_tokens, chunk.usage.completion_tokens
                if chunk.choices and chunk.choices[0].delta.content:
                    if not parts:
                        first_ms = int((time.monotonic() - t0) * 1000)
                    parts.append(chunk.choices[0].delta.content)
        finally:
            if hasattr(stream, "close"):
                try:
                    stream.close()
                except Exception:
                    pass

        text = "".join(parts)
        total = int((time.monotonic() - t0) * 1000)
        if not t_in:
            t_in, t_out = max(1, len((system + user)) // 4), max(1, len(text) // 4)
        return LLMResult(text=text, tokens_in=t_in, tokens_out=t_out, first_token_ms=first_ms, total_ms=total)


class ClaudeClient(LLMClient):
    """Client for Anthropic Claude models (using official anthropic SDK or streaming httpx fallback)."""

    def __init__(self, cfg: PortalConfig) -> None:
        self.cfg = cfg
        self.provider = canonical_provider(cfg.llm.provider)
        key, var_used = resolve_api_key(cfg)
        if not key:
            expected = get_provider_env_vars(self.provider, cfg.llm.api_key_env)
            raise RuntimeError(
                f"llm.provider is '{cfg.llm.provider}' but no API key was found in environment. "
                f"Checked environment variables: {', '.join(expected)}. "
                f"Set one (e.g. {expected[0]}=your-key), or use llm.provider: mock "
                "(PORTAL_LLM_PROVIDER=mock) for offline testing."
            )
        self.api_key = key
        self.model_version = cfg.llm.model or "claude-haiku-5-5"
        self.base_url = (cfg.llm.base_url or "https://api.anthropic.com").rstrip("/")
        self.timeout = cfg.llm.timeout_seconds

        import httpx
        max_p = getattr(cfg, "max_concurrent_participants", 100) or 100
        self._http_client = httpx.Client(
            timeout=self.timeout,
            limits=httpx.Limits(
                max_connections=max(100, max_p * 3),
                max_keepalive_connections=max(50, max_p),
            ),
        )
        self._anthropic_client = None
        try:
            import anthropic
            sdk_base_url = self.base_url
            if sdk_base_url.endswith("/v1"):
                sdk_base_url = sdk_base_url[:-3]
            self._anthropic_client = anthropic.Anthropic(
                api_key=self.api_key,
                base_url=sdk_base_url or None,
                timeout=self.timeout,
            )
        except (ImportError, Exception):
            self._anthropic_client = None

    def complete(self, system: str, user: str, *, temperature: float, max_tokens: int,
                 json_mode: bool = True, meta: dict[str, Any] | None = None) -> LLMResult:
        if self._anthropic_client is not None:
            try:
                return self._complete_sdk(system, user, temperature=temperature, max_tokens=max_tokens,
                                          json_mode=json_mode, meta=meta)
            except (ImportError, AttributeError, TypeError):
                # Fallback to direct HTTP only on SDK compatibility errors, not transient API/network errors
                self._anthropic_client = None
                return self._complete_http(system, user, temperature=temperature, max_tokens=max_tokens,
                                           json_mode=json_mode, meta=meta)
        return self._complete_http(system, user, temperature=temperature, max_tokens=max_tokens,
                                   json_mode=json_mode, meta=meta)

    def _complete_sdk(self, system: str, user: str, *, temperature: float, max_tokens: int,
                      json_mode: bool = True, meta: dict[str, Any] | None = None) -> LLMResult:
        t0 = time.monotonic()
        parts: list[str] = []
        first_ms = 0
        t_in = t_out = 0

        temp = min(1.0, max(0.0, temperature)) if getattr(self.cfg.llm, "send_temperature", True) else 1.0
        full_system = system
        if json_mode and "json" not in system.lower():
            full_system = (system + "\n\nCRITICAL: Respond ONLY with a valid JSON object.").strip()

        with self._anthropic_client.messages.stream(
            model=self.model_version,
            max_tokens=max_tokens,
            temperature=temp,
            system=full_system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            for text_chunk in stream.text_stream:
                if not parts:
                    first_ms = int((time.monotonic() - t0) * 1000)
                parts.append(text_chunk)
            msg = stream.get_final_message()
            if msg and msg.usage:
                t_in = getattr(msg.usage, "input_tokens", 0) or 0
                t_out = getattr(msg.usage, "output_tokens", 0) or 0

        text = "".join(parts)
        total = int((time.monotonic() - t0) * 1000)
        if not t_in:
            t_in, t_out = max(1, len((system + user)) // 4), max(1, len(text) // 4)
        return LLMResult(text=text, tokens_in=t_in, tokens_out=t_out, first_token_ms=first_ms, total_ms=total)

    def _complete_http(self, system: str, user: str, *, temperature: float, max_tokens: int,
                       json_mode: bool = True, meta: dict[str, Any] | None = None) -> LLMResult:
        import httpx
        import json

        t0 = time.monotonic()
        parts: list[str] = []
        first_ms = 0
        t_in = t_out = 0

        base = self.base_url
        if base.endswith("/v1"):
            url = f"{base}/messages"
        else:
            url = f"{base}/v1/messages"

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        temp = min(1.0, max(0.0, temperature)) if getattr(self.cfg.llm, "send_temperature", True) else 1.0
        full_system = system
        if json_mode and "json" not in system.lower():
            full_system = (system + "\n\nCRITICAL: Respond ONLY with a valid JSON object.").strip()

        payload = {
            "model": self.model_version,
            "max_tokens": max_tokens,
            "temperature": temp,
            "system": full_system,
            "messages": [{"role": "user", "content": user}],
            "stream": True,
        }

        if True:
            with self._http_client.stream("POST", url, headers=headers, json=payload) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            event_data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        event_type = event_data.get("type", "")
                        if event_type == "message_start":
                            msg_info = event_data.get("message", {})
                            usage = msg_info.get("usage", {})
                            t_in = usage.get("input_tokens", 0) or 0
                        elif event_type == "content_block_delta":
                            delta = event_data.get("delta", {})
                            if delta.get("type") == "text_delta":
                                chunk = delta.get("text", "")
                                if chunk:
                                    if not parts:
                                        first_ms = int((time.monotonic() - t0) * 1000)
                                    parts.append(chunk)
                        elif event_type == "message_delta":
                            usage = event_data.get("usage", {})
                            t_out = usage.get("output_tokens", 0) or 0

        text = "".join(parts)
        total = int((time.monotonic() - t0) * 1000)
        if not t_in:
            t_in, t_out = max(1, len((system + user)) // 4), max(1, len(text) // 4)
        return LLMResult(text=text, tokens_in=t_in, tokens_out=t_out, first_token_ms=first_ms, total_ms=total)


def make_llm(cfg: PortalConfig) -> LLMClient:
    canon = canonical_provider(cfg.llm.provider)
    if canon == "mock":
        from .mock_llm import MockClient
        return MockClient(cfg)
    if canon in ("claude", "anthropic"):
        return ClaudeClient(cfg)
    return OpenAICompatClient(cfg)


def call_with_retry(llm: LLMClient, agent: str, fn: Callable[[], Any], retries: int,
                    on_error: Callable[[str, str, int], None] | None = None) -> Any:
    """Run ``fn``; on failure retry up to ``retries`` times, logging each failure via ``on_error``."""
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - any model/network/parse failure counts
            if on_error:
                on_error(agent, type(exc).__name__, attempt)
            if attempt >= retries:
                raise AgentCallFailed(agent, type(exc).__name__, attempt) from exc
            attempt += 1


def format_transcript(messages: list[dict], limit: int | None = None) -> str:
    """Render the full team chat (folded blocks included) as plain text for a model prompt."""
    lines = []
    msgs = messages if limit is None else messages[-limit:]
    for m in msgs:
        to = ", ".join(m.get("to") or ["everyone"])
        lines.append(f"[{m['sender']} -> {to}]: {m['text']}")
    return "\n".join(lines) if lines else "(no messages yet)"
