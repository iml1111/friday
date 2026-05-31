"""AnthropicProvider adapter — normalization and exception mapping unit tests.

No real API calls. Uses SimpleNamespace stand-ins shaped like the actual anthropic SDK response
objects (measured against anthropic 0.89.0) to verify normalize(), and real anthropic exception
instances to verify the 5-class exception hierarchy mapping.

Stand-in shape (anthropic SDK):
  - response.content[i]: .type in {"text","tool_use","thinking"}
  - response.stop_reason: Optional[Literal["end_turn","max_tokens","stop_sequence","tool_use","pause_turn","refusal"]]
  - response.usage: .input_tokens/.output_tokens/.cache_creation_input_tokens/.cache_read_input_tokens
  - response.id / response.model
  - prompt-too-long surfaces as anthropic.BadRequestError(HTTP 400) with message "prompt is too long: ..."
"""
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from friday_agent.api.anthropic_provider import AnthropicProvider
from friday_agent.api.provider import (
    AssistantResponse,
    AuthError,
    ContextOverflowError,
    LLMError,
    RateLimitError,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    TransientError,
)


# ---------------------------------------------------------------------------
# SDK response stand-in builders (shaped like anthropic.types.Message)
# ---------------------------------------------------------------------------
def _sdk_text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text, citations=None)


def _sdk_tool_use_block(*, id: str, name: str, input: dict) -> SimpleNamespace:
    # The real SDK delivers input already parsed as a dict.
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input, caller=None)


def _sdk_thinking_block(*, thinking: str, signature: str) -> SimpleNamespace:
    return SimpleNamespace(type="thinking", thinking=thinking, signature=signature)


def _sdk_usage(
    *, input_tokens=10, output_tokens=20, cache_creation=0, cache_read=0
) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )


def _sdk_response(*, content, stop_reason, usage=None, id="msg_test", model="friday-x") -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        model=model,
        role="assistant",
        type="message",
        content=content,
        stop_reason=stop_reason,
        usage=usage if usage is not None else _sdk_usage(),
    )


# ---------------------------------------------------------------------------
# Anthropic native exception builders (real instances)
# ---------------------------------------------------------------------------
def _http_response(status: int, message: str) -> httpx.Response:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    return httpx.Response(status, request=req, json=body)


def _bad_request(message: str) -> anthropic.BadRequestError:
    resp = _http_response(400, message)
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    return anthropic.BadRequestError(message, response=resp, body=body)


def _rate_limit() -> anthropic.RateLimitError:
    resp = _http_response(429, "rate limit exceeded")
    return anthropic.RateLimitError("rate limit exceeded", response=resp, body=None)


def _auth_error() -> anthropic.AuthenticationError:
    resp = _http_response(401, "invalid x-api-key")
    return anthropic.AuthenticationError("invalid x-api-key", response=resp, body=None)


def _internal_error() -> anthropic.InternalServerError:
    resp = _http_response(500, "overloaded")
    return anthropic.InternalServerError("overloaded", response=resp, body=None)


# ---------------------------------------------------------------------------
# Normalization tests
# ---------------------------------------------------------------------------
def test_normalize_text_and_tool_use_blocks():
    """text + tool_use blocks → TextBlock + ToolUseBlock with dict input."""
    provider = AnthropicProvider(api_key="test-key")
    native = _sdk_response(
        content=[
            _sdk_text_block("hello"),
            _sdk_tool_use_block(id="toolu_1", name="search", input={"q": "weather", "n": 3}),
        ],
        stop_reason="tool_use",
        usage=_sdk_usage(input_tokens=100, output_tokens=50, cache_read=7),
        id="msg_01",
        model="friday-sonnet-4-5",
    )

    result = provider.normalize(native)

    assert isinstance(result, AssistantResponse)
    assert len(result.content) == 2

    text = result.content[0]
    assert isinstance(text, TextBlock)
    assert text.text == "hello"

    tool = result.content[1]
    assert isinstance(tool, ToolUseBlock)
    assert tool.id == "toolu_1"
    assert tool.name == "search"
    # SDK delivers input already as a dict; do not JSON-parse it again.
    assert isinstance(tool.input, dict)
    assert tool.input == {"q": "weather", "n": 3}

    assert result.stop_reason == StopReason.TOOL_USE

    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 50
    assert result.usage.cache_read_input_tokens == 7
    assert result.id == "msg_01"
    assert result.model == "friday-sonnet-4-5"


def test_normalize_end_turn_stop_reason():
    """stop_reason "end_turn" → StopReason.END_TURN."""
    provider = AnthropicProvider(api_key="test-key")
    native = _sdk_response(content=[_sdk_text_block("done")], stop_reason="end_turn")
    result = provider.normalize(native)
    assert result.stop_reason == StopReason.END_TURN
    assert len(result.content) == 1
    assert isinstance(result.content[0], TextBlock)


def test_normalize_max_tokens_stop_reason():
    """stop_reason "max_tokens" → StopReason.MAX_TOKENS."""
    provider = AnthropicProvider(api_key="test-key")
    native = _sdk_response(content=[_sdk_text_block("partial")], stop_reason="max_tokens")
    result = provider.normalize(native)
    assert result.stop_reason == StopReason.MAX_TOKENS


def test_normalize_thinking_block():
    """thinking block → ThinkingBlock(thinking, signature)."""
    provider = AnthropicProvider(api_key="test-key")
    native = _sdk_response(
        content=[
            _sdk_thinking_block(thinking="let me reason", signature="sig123"),
            _sdk_text_block("answer"),
        ],
        stop_reason="end_turn",
    )
    result = provider.normalize(native)
    assert isinstance(result.content[0], ThinkingBlock)
    assert result.content[0].thinking == "let me reason"
    assert result.content[0].signature == "sig123"
    assert isinstance(result.content[1], TextBlock)


def test_normalize_skips_unknown_block_types():
    """SDK content blocks of unrecognized types (e.g. server_tool_use) are silently dropped.

    Only text, tool_use, and thinking are part of the universal type set.
    """
    provider = AnthropicProvider(api_key="test-key")
    unknown = SimpleNamespace(type="server_tool_use", id="x", name="y", input={})
    native = _sdk_response(
        content=[_sdk_text_block("kept"), unknown],
        stop_reason="end_turn",
    )
    result = provider.normalize(native)
    assert len(result.content) == 1
    assert isinstance(result.content[0], TextBlock)
    assert result.content[0].text == "kept"


# ---------------------------------------------------------------------------
# Exception mapping tests
# ---------------------------------------------------------------------------
def test_map_rate_limit_error():
    """anthropic.RateLimitError(429) → RateLimitError."""
    provider = AnthropicProvider(api_key="test-key")
    mapped = provider.map_exception(_rate_limit())
    assert isinstance(mapped, RateLimitError)
    assert isinstance(mapped, LLMError)


def test_map_auth_error():
    """anthropic.AuthenticationError(401) → AuthError."""
    provider = AnthropicProvider(api_key="test-key")
    mapped = provider.map_exception(_auth_error())
    assert isinstance(mapped, AuthError)
    assert isinstance(mapped, LLMError)


def test_map_prompt_too_long_to_context_overflow():
    """prompt-too-long (BadRequestError 400 with matching message) → ContextOverflowError.

    The SDK has no dedicated exception for this case; the adapter must detect it via
    the message text so that the loop raises ContextOverflowError for caller-owned compaction recovery.
    """
    provider = AnthropicProvider(api_key="test-key")
    err = _bad_request("prompt is too long: 250000 tokens > 200000 maximum")
    mapped = provider.map_exception(err)
    assert isinstance(mapped, ContextOverflowError)
    assert isinstance(mapped, LLMError)


def test_map_context_window_exceeded_phrase_to_context_overflow():
    """'input length and max_tokens exceed context limit' 400 errors also map to ContextOverflowError."""
    provider = AnthropicProvider(api_key="test-key")
    err = _bad_request(
        "input length and `max_tokens` exceed context limit: 200000 + 8192 > 200000"
    )
    mapped = provider.map_exception(err)
    assert isinstance(mapped, ContextOverflowError)


def test_map_generic_bad_request_is_not_context_overflow():
    """A generic 400 BadRequest (not context-overflow) maps to LLMError, not ContextOverflowError."""
    provider = AnthropicProvider(api_key="test-key")
    err = _bad_request("messages: roles must alternate")
    mapped = provider.map_exception(err)
    assert isinstance(mapped, LLMError)
    assert not isinstance(mapped, ContextOverflowError)


def test_map_internal_server_error_is_transient():
    """5xx → TransientError (eligible for backoff retry)."""
    provider = AnthropicProvider(api_key="test-key")
    mapped = provider.map_exception(_internal_error())
    assert isinstance(mapped, TransientError)
    assert isinstance(mapped, LLMError)


def test_map_connection_error_is_transient():
    """Network connection/timeout errors → TransientError."""
    provider = AnthropicProvider(api_key="test-key")
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    mapped = provider.map_exception(anthropic.APIConnectionError(request=req))
    assert isinstance(mapped, TransientError)


def test_map_unknown_exception_is_llmerror():
    """Any unrecognized exception is wrapped as LLMError (the core loop only catches LLMError)."""
    provider = AnthropicProvider(api_key="test-key")
    mapped = provider.map_exception(ValueError("???"))
    assert isinstance(mapped, LLMError)
