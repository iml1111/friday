"""LLMConfig Protocol contract verification.

Enforces at runtime that all vendor configs structurally satisfy the LLMConfig Protocol
via isinstance (provider.py uses @runtime_checkable), since a type checker is not required.
"""
from friday_agent.api.configs import AnthropicConfig, OpenAIConfig
from friday_agent.api.provider import LLMConfig
from tests.fakes import FakeConfig


def test_anthropic_config_satisfies_protocol() -> None:
    assert isinstance(AnthropicConfig(), LLMConfig)


def test_openai_config_satisfies_protocol() -> None:
    assert isinstance(OpenAIConfig(), LLMConfig)


def test_fake_config_satisfies_protocol() -> None:
    assert isinstance(FakeConfig(), LLMConfig)
