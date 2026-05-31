"""OpenAIProvider adapter — normalization, message/tool conversion, and exception mapping unit tests.

No real API calls. Uses SimpleNamespace stand-ins shaped like OpenAI Chat Completions responses
to verify normalize(), the internal message conversion layer, and the 5-class exception mapping.
"""
import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from friday_agent.api.openai_provider import OpenAIProvider
from friday_agent.api.provider import (
    AssistantResponse,
    AuthError,
    ContextOverflowError,
    LLMError,
    RateLimitError,
    StopReason,
    TextBlock,
    ToolUseBlock,
    TransientError,
)


# ---------------------------------------------------------------------------
# OpenAI ChatCompletion response stand-in builders
# ---------------------------------------------------------------------------
def _oa_tool_call(*, id: str, name: str, arguments: str) -> SimpleNamespace:
    # OpenAI delivers arguments as a JSON string, not a dict.
    return SimpleNamespace(id=id, type="function", function=SimpleNamespace(name=name, arguments=arguments))


def _oa_message(*, content=None, tool_calls=None) -> SimpleNamespace:
    return SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)


def _oa_choice(*, message, finish_reason) -> SimpleNamespace:
    return SimpleNamespace(index=0, message=message, finish_reason=finish_reason)


def _oa_usage(*, prompt=10, completion=20, cached=0) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


def _oa_response(*, choices, usage=None, id="chatcmpl_test", model="gpt-x") -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        model=model,
        object="chat.completion",
        choices=choices,
        usage=usage if usage is not None else _oa_usage(),
    )


# ---------------------------------------------------------------------------
# openai native exception builders (real instances)
# ---------------------------------------------------------------------------
def _http_response(status: int, message: str, code: str | None = None) -> httpx.Response:
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    err: dict = {"message": message, "type": "invalid_request_error"}
    if code:
        err["code"] = code
    return httpx.Response(status, request=req, json={"error": err})


def _bad_request(message: str, code: str | None = None) -> openai.BadRequestError:
    err: dict = {"message": message, "type": "invalid_request_error"}
    if code:
        err["code"] = code
    return openai.BadRequestError(message, response=_http_response(400, message, code), body={"error": err})


def _rate_limit() -> openai.RateLimitError:
    return openai.RateLimitError("rate limit", response=_http_response(429, "rate limit"), body=None)


def _auth_error() -> openai.AuthenticationError:
    return openai.AuthenticationError("invalid key", response=_http_response(401, "invalid key"), body=None)


def _internal_error() -> openai.InternalServerError:
    return openai.InternalServerError("server error", response=_http_response(500, "server error"), body=None)


# ---------------------------------------------------------------------------
# Normalization tests
# ---------------------------------------------------------------------------
def test_normalize_text_and_tool_call():
    """text + tool_call → TextBlock + ToolUseBlock. JSON-string arguments are parsed to dict."""
    provider = OpenAIProvider(api_key="test-key")
    native = _oa_response(
        choices=[_oa_choice(
            message=_oa_message(
                content="thinking out loud",
                tool_calls=[_oa_tool_call(id="call_1", name="search", arguments='{"q": "weather", "n": 3}')],
            ),
            finish_reason="tool_calls",
        )],
        usage=_oa_usage(prompt=100, completion=50, cached=7),
        id="chatcmpl_01",
        model="gpt-4o",
    )

    result = provider.normalize(native)

    assert isinstance(result, AssistantResponse)
    assert len(result.content) == 2

    text = result.content[0]
    assert isinstance(text, TextBlock)
    assert text.text == "thinking out loud"

    tool = result.content[1]
    assert isinstance(tool, ToolUseBlock)
    assert tool.id == "call_1"
    assert tool.name == "search"
    # OpenAI arguments (JSON string) must be parsed to a dict.
    assert isinstance(tool.input, dict)
    assert tool.input == {"q": "weather", "n": 3}

    assert result.stop_reason == StopReason.TOOL_USE
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 50
    assert result.usage.cache_read_input_tokens == 7
    assert result.id == "chatcmpl_01"
    assert result.model == "gpt-4o"


def test_normalize_stop_finish_reason():
    provider = OpenAIProvider(api_key="test-key")
    native = _oa_response(choices=[_oa_choice(message=_oa_message(content="done"), finish_reason="stop")])
    result = provider.normalize(native)
    assert result.stop_reason == StopReason.END_TURN
    assert len(result.content) == 1
    assert isinstance(result.content[0], TextBlock)


def test_normalize_length_finish_reason():
    provider = OpenAIProvider(api_key="test-key")
    native = _oa_response(choices=[_oa_choice(message=_oa_message(content="partial"), finish_reason="length")])
    result = provider.normalize(native)
    assert result.stop_reason == StopReason.MAX_TOKENS


def test_normalize_invalid_tool_arguments_yields_empty_dict():
    """Malformed JSON arguments degrade gracefully to {} without raising."""
    provider = OpenAIProvider(api_key="test-key")
    native = _oa_response(
        choices=[_oa_choice(
            message=_oa_message(content=None, tool_calls=[_oa_tool_call(id="c1", name="f", arguments="{not json")]),
            finish_reason="tool_calls",
        )],
    )
    result = provider.normalize(native)
    assert len(result.content) == 1
    assert isinstance(result.content[0], ToolUseBlock)
    assert result.content[0].input == {}


def test_normalize_empty_choices():
    """Empty choices list yields empty content + END_TURN as a defensive fallback."""
    provider = OpenAIProvider(api_key="test-key")
    native = _oa_response(choices=[])
    result = provider.normalize(native)
    assert result.content == []
    assert result.stop_reason == StopReason.END_TURN


# ---------------------------------------------------------------------------
# Message/tool conversion tests (internal dict format → OpenAI format)
# ---------------------------------------------------------------------------
def test_to_openai_messages_full_roundtrip():
    """Converts system + user text + assistant(tool_use) + user(tool_result) to OpenAI format."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "calling"},
            {"type": "tool_use", "id": "call_1", "name": "search", "input": {"q": "x"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "result text"},
        ]},
    ]
    out = OpenAIProvider._to_openai_messages(messages, "SYSTEM PROMPT")

    assert out[0] == {"role": "system", "content": "SYSTEM PROMPT"}
    assert out[1] == {"role": "user", "content": "hi"}

    assistant = out[2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "calling"
    assert len(assistant["tool_calls"]) == 1
    tc = assistant["tool_calls"][0]
    assert tc["id"] == "call_1"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "search"
    # arguments must be serialized as a JSON string.
    assert json.loads(tc["function"]["arguments"]) == {"q": "x"}

    tool_msg = out[3]
    assert tool_msg == {"role": "tool", "tool_call_id": "call_1", "content": "result text"}


def test_to_openai_messages_no_system_prompt():
    out = OpenAIProvider._to_openai_messages(
        [{"role": "user", "content": [{"type": "text", "text": "hello"}]}], ""
    )
    assert out == [{"role": "user", "content": "hello"}]


def test_to_openai_messages_assistant_tool_only_has_null_content():
    """An assistant message with only tool_use and no text produces content=None + tool_calls."""
    out = OpenAIProvider._to_openai_messages(
        [{"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "f", "input": {}}]}], ""
    )
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] is None
    assert out[0]["tool_calls"][0]["id"] == "c1"


def test_to_openai_tools():
    tools = [{
        "name": "search",
        "description": "Search the web",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }]
    out = OpenAIProvider._to_openai_tools(tools)
    assert out == [{
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search the web",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }]


def test_to_openai_tools_empty():
    assert OpenAIProvider._to_openai_tools([]) == []


# ---------------------------------------------------------------------------
# Exception mapping tests
# ---------------------------------------------------------------------------
def test_map_context_length_exceeded_to_context_overflow():
    """context_length_exceeded error code maps to ContextOverflowError (triggers caller-owned compaction recovery)."""
    provider = OpenAIProvider(api_key="test-key")
    err = _bad_request("This model's maximum context length is 8192 tokens", code="context_length_exceeded")
    mapped = provider.map_exception(err)
    assert isinstance(mapped, ContextOverflowError)
    assert isinstance(mapped, LLMError)


def test_map_context_overflow_via_message_signal():
    """'maximum context length' in the error message alone maps to ContextOverflowError."""
    provider = OpenAIProvider(api_key="test-key")
    err = _bad_request("maximum context length exceeded: reduce the length of the messages")
    mapped = provider.map_exception(err)
    assert isinstance(mapped, ContextOverflowError)


def test_map_generic_bad_request_is_not_context_overflow():
    provider = OpenAIProvider(api_key="test-key")
    err = _bad_request("Invalid 'tools': must be a non-empty array")
    mapped = provider.map_exception(err)
    assert isinstance(mapped, LLMError)
    assert not isinstance(mapped, ContextOverflowError)


def test_map_rate_limit_error():
    provider = OpenAIProvider(api_key="test-key")
    mapped = provider.map_exception(_rate_limit())
    assert isinstance(mapped, RateLimitError)


def test_map_auth_error():
    provider = OpenAIProvider(api_key="test-key")
    mapped = provider.map_exception(_auth_error())
    assert isinstance(mapped, AuthError)


def test_map_internal_server_error_is_transient():
    provider = OpenAIProvider(api_key="test-key")
    mapped = provider.map_exception(_internal_error())
    assert isinstance(mapped, TransientError)


def test_map_connection_error_is_transient():
    provider = OpenAIProvider(api_key="test-key")
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    mapped = provider.map_exception(openai.APIConnectionError(request=req))
    assert isinstance(mapped, TransientError)


def test_map_unknown_exception_is_llmerror():
    provider = OpenAIProvider(api_key="test-key")
    mapped = provider.map_exception(ValueError("???"))
    assert isinstance(mapped, LLMError)
