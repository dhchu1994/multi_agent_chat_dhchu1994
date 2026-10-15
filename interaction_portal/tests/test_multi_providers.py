"""Tests for multi-provider LLM support: Claude, ChatGPT/OpenAI, DeepSeek, GLM, and Qwen."""

import os
from unittest.mock import MagicMock, patch
import pytest

from app.config_loader import (
    load_config,
    canonical_provider,
    get_provider_spec,
    get_provider_env_vars,
    resolve_api_key,
    PROVIDER_SPECS,
    PortalConfig,
    LlmConfig,
)
from app.agents.base import make_llm, ClaudeClient, OpenAICompatClient
from app.agents.mock_llm import MockClient


def test_provider_canonicalization():
    assert canonical_provider("openai") == "openai"
    assert canonical_provider("ChatGPT") == "openai"
    assert canonical_provider("claude") == "claude"
    assert canonical_provider("Anthropic") == "claude"
    assert canonical_provider("deepseek") == "deepseek"
    assert canonical_provider("glm") == "glm"
    assert canonical_provider("Zhipu") == "glm"
    assert canonical_provider("zhipuai") == "glm"
    assert canonical_provider("bigmodel") == "glm"
    assert canonical_provider("qwen") == "qwen"
    assert canonical_provider("DashScope") == "qwen"
    assert canonical_provider("tongyi") == "qwen"
    assert canonical_provider("mock") == "mock"
    assert canonical_provider("offline") == "mock"


def test_provider_specs_present():
    expected_providers = {"mock", "openai", "claude", "deepseek", "glm", "qwen"}
    assert set(PROVIDER_SPECS.keys()) == expected_providers
    for p in expected_providers:
        spec = get_provider_spec(p)
        assert spec is not None
        assert "base_url" in spec
        assert "api_key_env" in spec
        assert "fallback_api_key_envs" in spec
        assert "default_model" in spec


def test_get_provider_env_vars():
    claude_vars = get_provider_env_vars("claude")
    assert "ANTHROPIC_API_KEY" in claude_vars
    assert "CLAUDE_API_KEY" in claude_vars
    assert "PORTAL_LLM_API_KEY" in claude_vars

    openai_vars = get_provider_env_vars("openai")
    assert "OPENAI_API_KEY" in openai_vars
    assert "CHATGPT_API_KEY" in openai_vars
    assert "PORTAL_LLM_API_KEY" in openai_vars

    deepseek_vars = get_provider_env_vars("deepseek")
    assert "DEEPSEEK_API_KEY" in deepseek_vars
    assert "PORTAL_LLM_API_KEY" in deepseek_vars

    glm_vars = get_provider_env_vars("glm")
    assert "ZAI_API_KEY" in glm_vars
    assert "GLM_API_KEY" in glm_vars
    assert "ZHIPUAI_API_KEY" in glm_vars
    assert "BIGMODEL_API_KEY" in glm_vars
    assert "PORTAL_LLM_API_KEY" in glm_vars

    qwen_vars = get_provider_env_vars("qwen")
    assert "DASHSCOPE_API_KEY" in qwen_vars
    assert "QWEN_API_KEY" in qwen_vars
    assert "PORTAL_LLM_API_KEY" in qwen_vars


def test_resolve_api_key_with_fallbacks(monkeypatch):
    # Ensure clean env
    for k in [
        "OPENAI_API_KEY", "CHATGPT_API_KEY",
        "ANTHROPIC_API_KEY", "CLAUDE_API_KEY",
        "DEEPSEEK_API_KEY",
        "ZAI_API_KEY", "GLM_API_KEY", "ZHIPUAI_API_KEY", "BIGMODEL_API_KEY",
        "DASHSCOPE_API_KEY", "QWEN_API_KEY",
        "PORTAL_LLM_API_KEY",
    ]:
        monkeypatch.delenv(k, raising=False)

    cfg = load_config()

    # Test Claude with fallback CLAUDE_API_KEY
    cfg.set_provider("claude")
    monkeypatch.setenv("CLAUDE_API_KEY", "sk-claude-fallback")
    key, var = resolve_api_key(cfg)
    assert key == "sk-claude-fallback"
    assert var == "CLAUDE_API_KEY"

    # Test Claude with primary ANTHROPIC_API_KEY taking precedence
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-primary")
    key, var = resolve_api_key(cfg)
    assert key == "sk-ant-primary"
    assert var == "ANTHROPIC_API_KEY"

    # Test GLM with fallback ZHIPUAI_API_KEY
    cfg.set_provider("glm")
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.setenv("ZHIPUAI_API_KEY", "sk-zhipu-key")
    key, var = resolve_api_key(cfg)
    assert key == "sk-zhipu-key"
    assert var == "ZHIPUAI_API_KEY"

    # Test GLM with primary ZAI_API_KEY taking precedence
    monkeypatch.setenv("ZAI_API_KEY", "sk-zai-primary")
    key, var = resolve_api_key(cfg)
    assert key == "sk-zai-primary"
    assert var == "ZAI_API_KEY"

    # Test Qwen with QWEN_API_KEY
    cfg.set_provider("qwen")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("QWEN_API_KEY", "sk-qwen-key")
    key, var = resolve_api_key(cfg)
    assert key == "sk-qwen-key"
    assert var == "QWEN_API_KEY"

    # Test DeepSeek with DEEPSEEK_API_KEY
    cfg.set_provider("deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-key")
    key, var = resolve_api_key(cfg)
    assert key == "sk-ds-key"
    assert var == "DEEPSEEK_API_KEY"

    # Test universal PORTAL_LLM_API_KEY override
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("PORTAL_LLM_API_KEY", "sk-universal-key")
    key, var = resolve_api_key(cfg)
    assert key == "sk-universal-key"
    assert var == "PORTAL_LLM_API_KEY"


def test_load_config_env_provider_switch(monkeypatch):
    monkeypatch.setenv("PORTAL_LLM_PROVIDER", "deepseek")
    cfg = load_config()
    assert cfg.llm.provider == "deepseek"
    assert cfg.llm.base_url == "https://api.deepseek.com"
    assert cfg.llm.api_key_env == "DEEPSEEK_API_KEY"
    assert cfg.llm.model == "deepseek-flash"

    monkeypatch.setenv("PORTAL_LLM_PROVIDER", "claude")
    cfg = load_config()
    assert cfg.llm.provider == "claude"
    assert cfg.llm.base_url == "https://api.anthropic.com"
    assert cfg.llm.api_key_env == "ANTHROPIC_API_KEY"
    assert cfg.llm.model == "claude-haiku-5-5"

    monkeypatch.setenv("PORTAL_LLM_PROVIDER", "glm")
    cfg = load_config()
    assert cfg.llm.provider == "glm"
    assert cfg.llm.base_url == "https://api.z.ai/api/paas/v4"
    assert cfg.llm.api_key_env == "ZAI_API_KEY"
    assert cfg.llm.model == "glm-5.3-flash"

    monkeypatch.setenv("PORTAL_LLM_PROVIDER", "qwen")
    cfg = load_config()
    assert cfg.llm.provider == "qwen"
    assert cfg.llm.base_url == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    assert cfg.llm.api_key_env == "DASHSCOPE_API_KEY"
    assert cfg.llm.model == "qwen3.8-flash"

    monkeypatch.setenv("PORTAL_LLM_PROVIDER", "openai")
    cfg = load_config()
    assert cfg.llm.provider == "openai"
    assert cfg.llm.base_url == "https://api.openai.com/v1"
    assert cfg.llm.api_key_env == "OPENAI_API_KEY"
    assert cfg.llm.model == "gpt-5.4-mini"


def test_set_provider_helper():
    cfg = load_config()
    assert cfg.llm.provider == "mock"

    cfg.set_provider("deepseek")
    assert cfg.llm.provider == "deepseek"
    assert cfg.llm.base_url == "https://api.deepseek.com"
    assert cfg.llm.model == "deepseek-flash"
    assert cfg.llm.api_key_env == "DEEPSEEK_API_KEY"

    cfg.set_provider("claude")
    assert cfg.llm.provider == "claude"
    assert cfg.llm.base_url == "https://api.anthropic.com"
    assert cfg.llm.model == "claude-haiku-5-5"
    assert cfg.llm.api_key_env == "ANTHROPIC_API_KEY"

    cfg.set_provider("glm")
    assert cfg.llm.provider == "glm"
    assert cfg.llm.base_url == "https://api.z.ai/api/paas/v4"
    assert cfg.llm.model == "glm-5.3-flash"
    assert cfg.llm.api_key_env == "ZAI_API_KEY"

    cfg.set_provider("qwen")
    assert cfg.llm.provider == "qwen"
    assert cfg.llm.base_url == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    assert cfg.llm.model == "qwen3.8-flash"
    assert cfg.llm.api_key_env == "DASHSCOPE_API_KEY"

    cfg.set_provider("openai")
    assert cfg.llm.provider == "openai"
    assert cfg.llm.base_url == "https://api.openai.com/v1"
    assert cfg.llm.model == "gpt-5.4-mini"
    assert cfg.llm.api_key_env == "OPENAI_API_KEY"

    cfg.set_provider("mock")
    assert cfg.llm.provider == "mock"


def test_missing_api_key_error_messaging(monkeypatch):
    for k in [
        "OPENAI_API_KEY", "CHATGPT_API_KEY",
        "ANTHROPIC_API_KEY", "CLAUDE_API_KEY",
        "DEEPSEEK_API_KEY",
        "ZAI_API_KEY", "GLM_API_KEY", "ZHIPUAI_API_KEY", "BIGMODEL_API_KEY",
        "DASHSCOPE_API_KEY", "QWEN_API_KEY",
        "PORTAL_LLM_API_KEY",
    ]:
        monkeypatch.delenv(k, raising=False)

    cfg = load_config()

    cfg.set_provider("claude")
    with pytest.raises(RuntimeError) as exc_claude:
        make_llm(cfg)
    assert "claude" in str(exc_claude.value).lower()
    assert "ANTHROPIC_API_KEY" in str(exc_claude.value)

    cfg.set_provider("deepseek")
    with pytest.raises(RuntimeError) as exc_ds:
        make_llm(cfg)
    assert "deepseek" in str(exc_ds.value).lower()
    assert "DEEPSEEK_API_KEY" in str(exc_ds.value)

    cfg.set_provider("glm")
    with pytest.raises(RuntimeError) as exc_glm:
        make_llm(cfg)
    assert "glm" in str(exc_glm.value).lower()
    assert "ZAI_API_KEY" in str(exc_glm.value)

    cfg.set_provider("qwen")
    with pytest.raises(RuntimeError) as exc_qwen:
        make_llm(cfg)
    assert "qwen" in str(exc_qwen.value).lower()
    assert "DASHSCOPE_API_KEY" in str(exc_qwen.value)


def test_make_llm_routing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-claude-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-deepseek-key")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai-key")

    cfg = load_config()

    # Mock provider
    cfg.set_provider("mock")
    client = make_llm(cfg)
    assert isinstance(client, MockClient)

    # Claude provider
    cfg.set_provider("claude")
    client = make_llm(cfg)
    assert isinstance(client, ClaudeClient)
    assert client.api_key == "dummy-claude-key"

    # DeepSeek provider (uses OpenAICompatClient)
    cfg.set_provider("deepseek")
    client = make_llm(cfg)
    assert isinstance(client, OpenAICompatClient)
    assert client.provider == "deepseek"

    # OpenAI provider
    cfg.set_provider("openai")
    client = make_llm(cfg)
    assert isinstance(client, OpenAICompatClient)
    assert client.provider == "openai"


def test_claude_client_completion_mocked(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-key")
    cfg = load_config()
    cfg.set_provider("claude")

    client = ClaudeClient(cfg)

    # Mock the internal anthropic messages stream
    mock_msg = MagicMock()
    mock_msg.usage.input_tokens = 45
    mock_msg.usage.output_tokens = 20

    mock_stream = MagicMock()
    mock_stream.__enter__.return_value = mock_stream
    mock_stream.__exit__.return_value = None
    mock_stream.text_stream = ["{\"reply_text\": ", "\"Hello from Claude!\"}"]
    mock_stream.get_final_message.return_value = mock_msg

    client._anthropic_client = MagicMock()
    client._anthropic_client.messages.stream.return_value = mock_stream

    result = client.complete("System prompt", "User prompt", temperature=0.3, max_tokens=200, json_mode=True)
    assert result.text == '{"reply_text": "Hello from Claude!"}'
    assert result.tokens_in == 45
    assert result.tokens_out == 20
    assert result.total_ms >= 0


def test_openai_compat_client_completion_mocked(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-key")
    cfg = load_config()
    cfg.set_provider("deepseek")

    client = OpenAICompatClient(cfg)

    chunk1 = MagicMock()
    chunk1.usage = None
    chunk1.choices = [MagicMock(delta=MagicMock(content="{\"reply_text\": "))]

    chunk2 = MagicMock()
    chunk2.usage = MagicMock(prompt_tokens=30, completion_tokens=15)
    chunk2.choices = [MagicMock(delta=MagicMock(content="\"Hello from DeepSeek!\"}"))]

    mock_stream = [chunk1, chunk2]
    client._client = MagicMock()
    client._client.chat.completions.create.return_value = mock_stream

    result = client.complete("System prompt", "User prompt", temperature=0.3, max_tokens=200, json_mode=True)
    assert result.text == '{"reply_text": "Hello from DeepSeek!"}'
    assert result.tokens_in == 30
    assert result.tokens_out == 15


def test_claude_client_http_fallback_mocked(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-key")
    cfg = load_config()
    cfg.set_provider("claude")

    client = ClaudeClient(cfg)
    client._anthropic_client = None  # Force HTTP fallback

    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.iter_lines.return_value = [
        'event: message_start',
        'data: {"type": "message_start", "message": {"usage": {"input_tokens": 50}}}',
        '',
        'event: content_block_delta',
        'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "{\\"reply_text\\": \\"HTTP Claude\\"}"}}',
        '',
        'event: message_delta',
        'data: {"type": "message_delta", "usage": {"output_tokens": 25}}',
        '',
        'data: [DONE]',
    ]
    mock_response.__enter__.return_value = mock_response
    mock_response.__exit__.return_value = None

    mock_http_client = MagicMock()
    mock_http_client.__enter__.return_value = mock_http_client
    mock_http_client.__exit__.return_value = None
    mock_http_client.stream.return_value = mock_response

    client._http_client = mock_http_client
    with patch("httpx.Client", return_value=mock_http_client):
        res = client.complete("System", "User", temperature=0.3, max_tokens=100)
        assert res.text == '{"reply_text": "HTTP Claude"}'
        assert res.tokens_in == 50
        assert res.tokens_out == 25


def test_harness_provider_switch(tmp_path):
    from app.harnesses.harness_a import run_harness_a
    from app.harnesses.harness_b import run_harness_b

    passed_a, report_a = run_harness_a(client_id="C1", output_path=str(tmp_path / "a.json"), provider="mock")
    assert passed_a is True

    passed_b, report_b = run_harness_b(
        condition="EVA-TASK",
        client_id="C2",
        output_path=str(tmp_path / "b.json"),
        provider="mock",
        db_path=tmp_path / "multi_b.sqlite3",
    )
    assert passed_b is True
