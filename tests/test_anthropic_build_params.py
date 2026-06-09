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
    assert params["system"] == [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
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


# ── prompt caching: always-on ephemeral breakpoints ────────────────────────
# cache_control on the last system block (caches tools+system) and on the last
# block of the final + second-to-last messages (conversation history). The
# second-to-last message is the stable anchor: the turn-local todo reminder only
# ever lands on messages[-1].

_EPHEMERAL = {"type": "ephemeral"}


def test_build_params_caches_system_block():
    params = _provider()._build_params([], "sys", [], AnthropicConfig())
    assert params["system"][-1]["cache_control"] == _EPHEMERAL


def test_build_params_caches_conversation_messages():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "q1"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
        {"role": "user", "content": [{"type": "text", "text": "q2"}]},
    ]
    params = _provider()._build_params(messages, "sys", [], AnthropicConfig())
    assert params["messages"][-1]["content"][-1]["cache_control"] == _EPHEMERAL
    assert params["messages"][-2]["content"][-1]["cache_control"] == _EPHEMERAL
    assert "cache_control" not in params["messages"][-3]["content"][-1]  # earlier untouched


def test_build_params_second_to_last_anchor_stable():
    # messages[-1] carries an extra reminder-like trailing block; the always-stable
    # anchor messages[-2] must still carry the breakpoint.
    messages = [
        {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
        {"role": "user", "content": [
            {"type": "text", "text": "real"},
            {"type": "text", "text": "<system-reminder>todos</system-reminder>"},
        ]},
    ]
    params = _provider()._build_params(messages, "sys", [], AnthropicConfig())
    assert params["messages"][-2]["content"][-1]["cache_control"] == _EPHEMERAL
    assert params["messages"][-1]["content"][-1]["cache_control"] == _EPHEMERAL


def test_build_params_cache_with_empty_system():
    messages = [{"role": "user", "content": [{"type": "text", "text": "q"}]}]
    params = _provider()._build_params(messages, "", [], AnthropicConfig())
    assert params["system"] == []  # no system block, no system breakpoint
    assert params["messages"][-1]["content"][-1]["cache_control"] == _EPHEMERAL


def test_build_params_single_message_marks_only_last():
    messages = [{"role": "user", "content": [{"type": "text", "text": "q"}]}]
    params = _provider()._build_params(messages, "sys", [], AnthropicConfig())
    assert len(params["messages"]) == 1  # no IndexError reaching for messages[-2]
    assert params["messages"][-1]["content"][-1]["cache_control"] == _EPHEMERAL


def test_cache_control_coexists_with_tools_and_thinking():
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    params = _provider()._build_params([], "sys", tools, AnthropicConfig(thinking_enabled=True))
    assert params["system"][-1]["cache_control"] == _EPHEMERAL
    assert params["tools"] == tools  # tools cached via the system breakpoint, not marked themselves
    assert "temperature" not in params
