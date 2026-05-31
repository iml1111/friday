from friday_agent.api.configs import AnthropicConfig, OpenAIConfig


def test_anthropic_provider_config_type():
    from friday_agent.api.anthropic_provider import AnthropicProvider
    assert AnthropicProvider.config_type is AnthropicConfig


def test_openai_provider_config_type():
    from friday_agent.api.openai_provider import OpenAIProvider
    assert OpenAIProvider.config_type is OpenAIConfig


def test_fake_provider_config_type():
    from tests.fakes import FakeLLMProvider, FakeConfig
    assert FakeLLMProvider.config_type is FakeConfig
