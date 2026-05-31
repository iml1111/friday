import pytest

from friday_agent.api.provider import (
    AuthError,
    ContextOverflowError,
    LLMError,
    LLMProvider,
    RateLimitError,
    StopReason,
    TransientError,
)


def test_stop_reason_values():
    assert StopReason.END_TURN == "end_turn"
    assert StopReason.TOOL_USE == "tool_use"
    assert StopReason.CONTEXT_WINDOW_EXCEEDED == "model_context_window_exceeded"


def test_error_hierarchy():
    for sub in (RateLimitError, ContextOverflowError, AuthError, TransientError):
        assert issubclass(sub, LLMError)


def test_llmprovider_is_abstract():
    with pytest.raises(TypeError):
        LLMProvider()   # abstract: complete not implemented


def test_anthropic_provider_requires_api_key():
    """api_key is required; omitting it raises TypeError (no env-var fallback)."""
    from friday_agent.api.anthropic_provider import AnthropicProvider
    with pytest.raises(TypeError):
        AnthropicProvider()  # type: ignore[call-arg]


def test_openai_provider_requires_api_key():
    from friday_agent.api.openai_provider import OpenAIProvider
    with pytest.raises(TypeError):
        OpenAIProvider()  # type: ignore[call-arg]


def test_anthropic_provider_empty_api_key_raises():
    """Empty string api_key raises ValueError immediately (fail-fast)."""
    from friday_agent.api.anthropic_provider import AnthropicProvider
    with pytest.raises(ValueError):
        AnthropicProvider(api_key="")


def test_openai_provider_empty_api_key_raises():
    from friday_agent.api.openai_provider import OpenAIProvider
    with pytest.raises(ValueError):
        OpenAIProvider(api_key="")
