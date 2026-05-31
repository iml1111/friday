import pytest

from friday_agent.api.configs import AnthropicConfig, OpenAIConfig


def test_anthropic_config_defaults():
    c = AnthropicConfig()
    assert c.max_tokens == 16384
    assert c.temperature is None
    assert c.thinking_enabled is False
    assert c.thinking_budget is None


def test_openai_config_defaults():
    c = OpenAIConfig()
    assert c.max_tokens == 16384
    assert c.temperature is None


def test_openai_config_rejects_anthropic_only_field():
    # OpenAIConfig has no thinking_enabled field → TypeError on construction.
    with pytest.raises(TypeError):
        OpenAIConfig(thinking_enabled=True)


def test_anthropic_config_has_no_vendor_extensions():
    with pytest.raises(TypeError):
        AnthropicConfig(vendor_extensions={"x": 1})
