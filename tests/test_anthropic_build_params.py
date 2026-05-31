from friday_agent.api.anthropic_provider import AnthropicProvider
from friday_agent.api.configs import AnthropicConfig


def _provider():
    # No real API calls — _build_params does not touch the client.
    return AnthropicProvider(api_key="test-key", model="claude-sonnet-4-6")


def test_build_params_uses_default_model_and_max_tokens():
    p = _provider()
    params = p._build_params([], "sys", [], AnthropicConfig(max_tokens=512))
    assert params["model"] == "claude-sonnet-4-6"
    assert params["max_tokens"] == 512
    assert params["system"] == [{"type": "text", "text": "sys"}]
    assert params["stream"] is False


def test_build_params_thinking_omits_temperature():
    p = _provider()
    params = p._build_params(
        [], "", [], AnthropicConfig(thinking_enabled=True, thinking_budget=1024, temperature=0.7)
    )
    assert params["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert "temperature" not in params  # temperature must be omitted when thinking is enabled


def test_build_params_temperature_when_no_thinking():
    p = _provider()
    params = p._build_params([], "", [], AnthropicConfig(temperature=0.3))
    assert params["temperature"] == 0.3
    assert "thinking" not in params


def test_build_params_omits_empty_tools():
    p = _provider()
    params = p._build_params([], "", [], AnthropicConfig())
    assert "tools" not in params
