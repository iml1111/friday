"""Termination points and tool_result backfill tests.

Verifies the agent-loop exit paths (model_error, context overflow) and the
pairing invariant: every tool_use block in the final assistant message must have
a matching synthetic tool_result when the loop exits without executing the tools.
"""
import pytest

from friday_agent.api.provider import (
    AssistantResponse,
    ContextOverflowError,
    LLMError,
    RateLimitError,
    StopReason,
    TextBlock,
    TokenUsage,
    ToolUseBlock,
    TransientError,
)
from tests._drive import drive
from friday_agent.core.state import Terminal
from friday_agent.messages.types import Message, create_user_message
from friday_agent.tools.builtin.example_tool import ExampleTool
from tests.fakes import FakeLLMProvider


# ---------------------------------------------------------------------------
# Helpers: extract tool_use / tool_result id sets from the yielded stream.
# ---------------------------------------------------------------------------

def _collect_tool_use_ids(items: list) -> set[str]:
    """Return the set of tool_use block ids from yielded assistant Messages."""
    ids: set[str] = set()
    for item in items:
        if isinstance(item, Message) and item.type == "assistant":
            for block in item.content:
                if block.type == "tool_use" and block.id:
                    ids.add(block.id)
    return ids


def _collect_tool_result_ids(items: list) -> set[str]:
    """Return the set of tool_use_ids referenced by tool_result blocks in yielded Messages."""
    ids: set[str] = set()
    for item in items:
        if isinstance(item, Message):
            for block in item.content:
                if block.type == "tool_result" and block.tool_use_id:
                    ids.add(block.tool_use_id)
    return ids


def _tool_use_response(tool_id: str) -> AssistantResponse:
    """Return an AssistantResponse that requests ExampleTool (mutating=False, so calls are concurrency-safe)."""
    tu = ToolUseBlock(id=tool_id, name="ExampleTool", input={"payload": "x", "mutating": False})
    return AssistantResponse(content=[tu], stop_reason=StopReason.TOOL_USE, usage=TokenUsage())


# ===========================================================================
# model_error — non-ContextOverflow LLMError during API call
# ===========================================================================

@pytest.mark.asyncio
async def test_model_error_terminates_with_error():
    """provider.complete() raising LLMError yields Terminal(reason='model_error', error=...)."""
    err = RateLimitError("rate limited")
    fake = FakeLLMProvider(responses=[], errors={0: err})

    items = []
    async for item in drive(
        provider=fake,
        tools=[ExampleTool()],
        messages=[create_user_message("trigger error")],
    ):
        items.append(item)

    terminal = items[-1]
    assert isinstance(terminal, Terminal)
    assert terminal.reason == "model_error"
    assert terminal.error is err


@pytest.mark.asyncio
async def test_model_error_generic_llm_error():
    """Base LLMError (not a subclass) is also classified as model_error."""
    err = LLMError("boom")
    fake = FakeLLMProvider(responses=[], errors={0: err})

    items = []
    async for item in drive(
        provider=fake,
        tools=[ExampleTool()],
        messages=[create_user_message("error")],
    ):
        items.append(item)

    terminal = items[-1]
    assert isinstance(terminal, Terminal)
    assert terminal.reason == "model_error"
    assert terminal.error is err


@pytest.mark.asyncio
async def test_model_error_after_tool_use_backfills():
    """Pairing invariant holds when model_error fires on the turn after a completed tool call.

    Turn 1: tool_use(t1) is executed and its tool_result is produced.
    Turn 2: API raises an error — the previous assistant message carried no pending
    tool_use, so there is nothing to backfill. Zero unmatched tool_use ids.
    """
    fake = FakeLLMProvider(
        responses=[_tool_use_response("t1")],
        errors={1: TransientError("network down")},
    )

    items = []
    async for item in drive(
        provider=fake,
        tools=[ExampleTool()],
        messages=[create_user_message("tool then error")],
    ):
        items.append(item)

    terminal = items[-1]
    assert isinstance(terminal, Terminal)
    assert terminal.reason == "model_error"

    use_ids = _collect_tool_use_ids(items)
    result_ids = _collect_tool_result_ids(items)
    assert use_ids.issubset(result_ids), f"unmatched tool_use ids: {use_ids - result_ids}"


# ===========================================================================
# ContextOverflowError boundary — must not be swallowed as model_error
# ===========================================================================

@pytest.mark.asyncio
async def test_context_overflow_not_swallowed_as_model_error():
    """ContextOverflowError propagates to the caller (drive), which compacts and retries; it is not classified as model_error.

    call 0: ContextOverflowError (injected) → drive catches → engine.compact
    call 1: compact summary call → <summary> response
    call 2: retry → end_turn → Terminal(completed)
    """
    err = ContextOverflowError("prompt too long")
    summary_resp = AssistantResponse(
        content=[TextBlock(text="<summary>recovered</summary>")],
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(),
    )
    end_resp = AssistantResponse(
        content=[TextBlock(text="ok")],
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(),
    )
    fake = FakeLLMProvider(responses=[summary_resp, end_resp], errors={0: err})

    items = []
    async for item in drive(
        provider=fake,
        tools=[ExampleTool()],
        messages=[create_user_message("context overflow")],
    ):
        items.append(item)

    terminal = items[-1]
    assert isinstance(terminal, Terminal)
    assert terminal.reason != "model_error", "ContextOverflowError must not be swallowed as model_error"
    assert terminal.reason == "completed", "loop should complete normally after the caller compacts and retries"
