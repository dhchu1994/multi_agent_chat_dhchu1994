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

from ..config_loader import PortalConfig


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
    def __init__(self, cfg: PortalConfig) -> None:
        from openai import OpenAI

        key = os.environ.get(cfg.llm.api_key_env, "")
        if not key:
            raise RuntimeError(
                f"llm.provider is 'openai' but the environment variable {cfg.llm.api_key_env} is empty. "
                "Set it, or use llm.provider: mock (PORTAL_LLM_PROVIDER=mock) for offline testing."
            )
        self._client = OpenAI(base_url=cfg.llm.base_url or None, api_key=key, timeout=cfg.llm.timeout_seconds)
        self.model_version = cfg.llm.model

    def complete(self, system: str, user: str, *, temperature: float, max_tokens: int,
                 json_mode: bool = True, meta: dict[str, Any] | None = None) -> LLMResult:
        t0 = time.monotonic()
        kwargs: dict[str, Any] = dict(
            model=self.model_version, temperature=temperature, max_tokens=max_tokens, stream=True,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            kwargs["stream_options"] = {"include_usage": True}
            stream = self._client.chat.completions.create(**kwargs)
        except TypeError:
            kwargs.pop("stream_options", None)
            stream = self._client.chat.completions.create(**kwargs)
        parts: list[str] = []
        first_ms = 0
        t_in = t_out = 0
        for chunk in stream:
            if getattr(chunk, "usage", None):
                t_in, t_out = chunk.usage.prompt_tokens, chunk.usage.completion_tokens
            if chunk.choices and chunk.choices[0].delta.content:
                if not parts:
                    first_ms = int((time.monotonic() - t0) * 1000)
                parts.append(chunk.choices[0].delta.content)
        text = "".join(parts)
        total = int((time.monotonic() - t0) * 1000)
        if not t_in:
            t_in, t_out = max(1, len((system + user)) // 4), max(1, len(text) // 4)
        return LLMResult(text=text, tokens_in=t_in, tokens_out=t_out, first_token_ms=first_ms, total_ms=total)


def make_llm(cfg: PortalConfig) -> LLMClient:
    if cfg.llm.provider == "mock":
        from .mock_llm import MockClient
        return MockClient(cfg)
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


def format_transcript(messages: list[dict], limit: int = 60) -> str:
    """Render the full team chat (folded blocks included) as plain text for a model prompt."""
    lines = []
    for m in messages[-limit:]:
        to = ", ".join(m.get("to") or ["everyone"])
        lines.append(f"[{m['sender']} -> {to}]: {m['text']}")
    return "\n".join(lines) if lines else "(no messages yet)"
