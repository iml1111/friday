"""Context-overflow recovery tests for the caller-owned compaction model.

step() no longer compacts internally: on a provider ContextOverflowError it
propagates the error. The caller recovers by calling engine.compact(state) and
retrying step(). tests/_drive.py embeds exactly this loop.

Call-index note: engine.compact() calls provider.complete() once (the summary
call), so it consumes a call slot:
  call 0: first step's API call → ContextOverflowError injected
  call 1: engine.compact summary call → summary response
  call 2: retry step after compact → end_turn → Terminal(completed)
"""
import pytest

from friday_agent.api.provider import (
    AssistantResponse,
    ContextOverflowError,
    StopReason,
    TextBlock,
    TokenUsage,
)
from friday_agent.core.engine import QueryEngine
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import create_user_message
from tests._drive import drive
from tests.fakes import FakeLLMProvider


def _summary_response(summary_text: str) -> AssistantResponse:
    return AssistantResponse(
        content=[TextBlock(text=f"<analysis>x</analysis><summary>{summary_text}</summary>")],
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(input_tokens=100, output_tokens=50),
    )


def _end_turn_response(text: str = "final answer") -> AssistantResponse:
    return AssistantResponse(
        content=[TextBlock(text=text)],
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


@pytest.mark.asyncio
async def test_step_raises_context_overflow():
    """step() propagates ContextOverflowError instead of recovering internally."""
    fake = FakeLLMProvider(responses=[], errors={0: ContextOverflowError("prompt too long")})
    engine = QueryEngine(provider=fake, tools=[])

    with pytest.raises(ContextOverflowError):
        async for _ in engine.step(LoopState(messages=[create_user_message("assume a long conversation")])):
            pass


@pytest.mark.asyncio
async def test_compact_shrinks_to_single_summary():
    """engine.compact() summarizes history into one summary message, preserving turn_count."""
    fake = FakeLLMProvider(responses=[_summary_response("SHRUNK")])
    engine = QueryEngine(provider=fake, tools=[])
    state = LoopState(
        messages=[create_user_message("a"), create_user_message("b"), create_user_message("c")],
        turn_count=4,
    )

    new_state = await engine.compact(state)

    assert len(new_state.messages) == 1
    assert new_state.messages[0].is_compact_summary is True
    summary_text = " ".join(b.text or "" for b in new_state.messages[0].content if b.text)
    assert "SHRUNK" in summary_text
    assert new_state.turn_count == 4


@pytest.mark.asyncio
async def test_overflow_then_compact_then_retry_completes():
    """drive() catches the overflow, compacts, retries, and completes."""
    fake = FakeLLMProvider(
        responses=[_summary_response("RECOVERED"), _end_turn_response()],
        errors={0: ContextOverflowError("prompt too long")},
    )

    items = []
    async for item in drive(
        provider=fake,
        tools=[],
        messages=[create_user_message("assume a long conversation")],
    ):
        items.append(item)

    terminal = items[-1]
    assert isinstance(terminal, Terminal)
    assert terminal.reason == "completed"

    # overflow(0) + compact summary(1) + retry(2)
    assert fake.call_count == 3

    def _flatten_text(messages):
        chunks = []
        for msg in messages:
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(block.get("text") or "")
        return " ".join(chunks)

    assert "RECOVERED" in _flatten_text(fake.received_messages[2])
