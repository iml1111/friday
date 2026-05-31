import pytest

from friday_agent.api.provider import (
    AssistantResponse,
    ContextOverflowError,
    StopReason,
    TokenUsage,
)
from tests.fakes import FakeLLMProvider


@pytest.mark.asyncio
async def test_scripts_responses_in_order():
    r1 = AssistantResponse(content=[], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    fake = FakeLLMProvider(responses=[r1])
    got = await fake.complete(messages=[], system_prompt="", tools=[], config=None)
    assert got.stop_reason == StopReason.END_TURN
    assert fake.call_count == 1


@pytest.mark.asyncio
async def test_can_raise_injected_error():
    fake = FakeLLMProvider(responses=[], errors=[ContextOverflowError()])
    with pytest.raises(ContextOverflowError):
        await fake.complete(messages=[], system_prompt="", tools=[], config=None)


@pytest.mark.asyncio
async def test_responses_returned_in_order():
    r1 = AssistantResponse(content=[], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    r2 = AssistantResponse(content=[], stop_reason=StopReason.TOOL_USE, usage=TokenUsage())
    fake = FakeLLMProvider(responses=[r1, r2])
    got1 = await fake.complete(messages=[], system_prompt="", tools=[], config=None)
    got2 = await fake.complete(messages=[], system_prompt="", tools=[], config=None)
    assert got1.stop_reason == StopReason.END_TURN
    assert got2.stop_reason == StopReason.TOOL_USE
    assert fake.call_count == 2


@pytest.mark.asyncio
async def test_error_on_specific_call_index_then_success():
    """Error injected at a specific call index; subsequent calls return success (caller-owned compaction recovery scenario)."""
    r1 = AssistantResponse(content=[], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    fake = FakeLLMProvider(
        responses=[r1],
        errors={0: ContextOverflowError()},
    )
    with pytest.raises(ContextOverflowError):
        await fake.complete(messages=[], system_prompt="", tools=[], config=None)
    # call index 1 → normal response
    got = await fake.complete(messages=[], system_prompt="", tools=[], config=None)
    assert got.stop_reason == StopReason.END_TURN
    assert fake.call_count == 2


@pytest.mark.asyncio
async def test_received_messages_captured():
    """Messages passed to complete() are recorded in received_messages."""
    r1 = AssistantResponse(content=[], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    fake = FakeLLMProvider(responses=[r1])
    msgs = [{"role": "user", "content": "hello"}]
    await fake.complete(messages=msgs, system_prompt="sys", tools=[], config=None)
    assert fake.received_messages[0] == msgs
