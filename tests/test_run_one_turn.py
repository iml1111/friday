"""core/loop.py run_one_turn() — unit tests for the single-turn step function."""
import pytest

from friday_agent.api.prompts import GENERAL_AGENT_GUIDANCE
from friday_agent.api.provider import (
    AssistantResponse, StopReason, TextBlock, ToolUseBlock, TokenUsage,
)
from friday_agent.core.loop import run_one_turn
from friday_agent.core.state import Checkpoint, LoopState, Terminal
from friday_agent.messages.types import create_user_message
from friday_agent.tools.builtin.example_tool import ExampleTool
from tests.fakes import FakeLLMProvider


async def _drain(agen):
    """Drains run_one_turn; returns (yielded Messages, final sentinel)."""
    items = []
    async for it in agen:
        items.append(it)
    assert items, "run_one_turn must yield at least one sentinel"
    return items[:-1], items[-1]


def _run_one(provider, tools, state):
    tool_schemas = [t.get_tool_schema() for t in tools]
    return run_one_turn(
        provider=provider, tools=tools, tool_schemas=tool_schemas, state=state,
        system_prompt="", config=None,
        max_concurrency=10,
    )


@pytest.mark.asyncio
async def test_run_one_turn_end_turn_yields_terminal_completed():
    end = AssistantResponse(content=[TextBlock(text="hello")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    provider = FakeLLMProvider(responses=[end])
    state = LoopState(messages=[create_user_message("hi")])

    messages, sentinel = await _drain(_run_one(provider, [], state))

    assert isinstance(sentinel, Terminal)
    assert sentinel.reason == "completed"
    assert any(b.type == "text" and b.text == "hello" for m in messages for b in m.content)


@pytest.mark.asyncio
async def test_run_one_turn_tool_use_yields_checkpoint():
    tool_use = AssistantResponse(
        content=[ToolUseBlock(id="tu1", name="ExampleTool", input={"payload": "x", "mutating": False})],
        stop_reason=StopReason.TOOL_USE, usage=TokenUsage(),
    )
    provider = FakeLLMProvider(responses=[tool_use])
    state = LoopState(messages=[create_user_message("run")])

    messages, sentinel = await _drain(_run_one(provider, [ExampleTool()], state))

    assert isinstance(sentinel, Checkpoint)
    assert sentinel.state.turn_count == 2
    assert any(b.type == "tool_use" for m in sentinel.state.messages for b in m.content)
    assert any(b.type == "tool_result" for m in sentinel.state.messages for b in m.content)
    assert any(b.type == "tool_result" for m in messages for b in m.content)


@pytest.mark.asyncio
async def test_run_one_turn_injects_general_guidance():
    """run_one_turn appends GENERAL_AGENT_GUIDANCE after the caller's system prompt."""
    end = AssistantResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    provider = FakeLLMProvider(responses=[end])
    state = LoopState(messages=[create_user_message("hi")])
    agen = run_one_turn(
        provider=provider, tools=[], tool_schemas=[], state=state,
        system_prompt="You are a research assistant.",
        config=None, max_concurrency=10,
    )
    await _drain(agen)

    sent = provider.received_system_prompts[0]
    assert sent.startswith("You are a research assistant.")
    assert GENERAL_AGENT_GUIDANCE in sent


