import pytest

from friday_agent.api.configs import AnthropicConfig
from friday_agent.core.engine import FridayAgent
from tests.fakes import FakeConfig, FakeLLMProvider


def test_provider_path_rejects_mismatched_config():
    # FakeLLMProvider expects FakeConfig but AnthropicConfig is supplied → ValueError on construction.
    fake = FakeLLMProvider(responses=[])
    with pytest.raises(ValueError):
        FridayAgent(provider=fake, config=AnthropicConfig())


def test_provider_path_accepts_matching_config():
    fake = FakeLLMProvider(responses=[])
    engine = FridayAgent(provider=fake, config=FakeConfig(max_tokens=512))
    assert isinstance(engine._config, FakeConfig)
    assert engine._config.max_tokens == 512


def test_provider_path_defaults_config_to_provider_type():
    fake = FakeLLMProvider(responses=[])
    engine = FridayAgent(provider=fake)
    assert isinstance(engine._config, FakeConfig)
