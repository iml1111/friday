"""Unit tests for scripts/_env.create_config — prefix-routed vendor config factory."""
import sys
from pathlib import Path

import pytest

# scripts/_env.py lives outside the friday_agent package; put scripts/ on the path
# the same way run_agent.py does. create_config touches no env vars / API keys.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from _env import create_config  # noqa: E402
from friday_agent.api.configs import AnthropicConfig, OpenAIConfig  # noqa: E402


def test_claude_prefix_builds_anthropic_config():
    cfg = create_config("claude-sonnet-4-6", max_tokens=512)
    assert isinstance(cfg, AnthropicConfig)
    assert cfg.max_tokens == 512


def test_gpt_prefix_builds_openai_config():
    cfg = create_config("gpt-4o", max_tokens=256, temperature=0.5)
    assert isinstance(cfg, OpenAIConfig)
    assert cfg.max_tokens == 256
    assert cfg.temperature == 0.5


def test_no_kwargs_uses_vendor_defaults():
    cfg = create_config("claude-sonnet-4-6")
    assert isinstance(cfg, AnthropicConfig)
    assert cfg.max_tokens == 16384  # AnthropicConfig default


def test_unsupported_prefix_raises_value_error():
    with pytest.raises(ValueError):
        create_config("gemini-1.5-pro", max_tokens=512)


def test_empty_model_raises_value_error():
    with pytest.raises(ValueError):
        create_config("", max_tokens=512)


def test_vendor_unsupported_field_raises_type_error():
    # thinking_enabled is Anthropic-only; OpenAIConfig must reject it (fail-fast).
    with pytest.raises(TypeError):
        create_config("gpt-4o", thinking_enabled=True)
