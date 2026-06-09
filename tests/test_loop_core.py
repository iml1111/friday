"""Core agent-loop tests driven via tests/_drive.py (drive()).

drive() loops FridayAgent.step() to completion, streaming Messages and yielding
a Terminal as the final item. The caller detects completion by seeing a Terminal.
"""
import pytest

from friday_agent.api.provider import (
    AssistantResponse,
    StopReason,
    TextBlock,
    ThinkingBlock,
    TokenUsage,
    ToolUseBlock,
)
from tests._drive import drive
from friday_agent.core.state import Terminal
from friday_agent.tools.builtin.example_tool import ExampleTool
from friday_agent.messages.types import create_user_message
from tests.fakes import FakeLLMProvider


@pytest.mark.asyncio
async def test_tool_use_then_end_turn():
    # turn 1: tool_use(ExampleTool); turn 2: end_turn text.
    tu = ToolUseBlock(id="t1", name="ExampleTool", input={"payload": "hi", "mutating": False})
    r1 = AssistantResponse(content=[tu], stop_reason=StopReason.TOOL_USE, usage=TokenUsage())
    r2 = AssistantResponse(content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    fake = FakeLLMProvider(responses=[r1, r2])

    items = []
    async for item in drive(
        provider=fake,
        tools=[ExampleTool()],
        messages=[create_user_message("Process 'hi' with ExampleTool.")],
    ):
        items.append(item)

    # Loop terminates normally: last yielded item is Terminal(reason="completed").
    terminal = items[-1]
    assert isinstance(terminal, Terminal)
    assert terminal.reason == "completed"

    # complete() called exactly twice.
    assert fake.call_count == 2

    # Tool executed and its tool_result was fed back in the second call's messages.
    second_call_messages = fake.received_messages[1]
    tool_results = [
        block
        for msg in second_call_messages
        for block in msg.get("content", [])
        if block.get("type") == "tool_result"
    ]
    assert any(block.get("tool_use_id") == "t1" for block in tool_results)


@pytest.mark.asyncio
async def test_thinking_signature_echoed_on_next_turn():
    # Reproduces the reasoning round-trip bug: a thinking block emitted on turn 1
    # must be echoed back WITH its signature on turn 2, or the API rejects the turn
    # (400 "thinking.signature: Field required").
    think = ThinkingBlock(thinking="reason", signature="sig123")
    tu = ToolUseBlock(id="t1", name="ExampleTool", input={"payload": "hi", "mutating": False})
    r1 = AssistantResponse(content=[think, tu], stop_reason=StopReason.TOOL_USE, usage=TokenUsage())
    r2 = AssistantResponse(content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    fake = FakeLLMProvider(responses=[r1, r2])

    async for _ in drive(
        provider=fake,
        tools=[ExampleTool()],
        messages=[create_user_message("Process 'hi' with ExampleTool.")],
    ):
        pass

    assert fake.call_count == 2
    # The assistant message echoed back on the SECOND call must carry the signed thinking block.
    thinking_blocks = [
        block
        for msg in fake.received_messages[1]
        for block in msg.get("content", [])
        if block.get("type") == "thinking"
    ]
    assert thinking_blocks, "prior thinking block must be echoed on the 2nd call"
    assert thinking_blocks[0].get("thinking") == "reason"
    assert thinking_blocks[0].get("signature") == "sig123"
