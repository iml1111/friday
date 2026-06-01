"""Tests for SDK built-in tool auto-registration and guidance (always-on, no opt-out).

Import discipline: this file must NOT import TODO_GUIDANCE — guidance assertions
live in tests/test_prompts.py and tests/test_engine.py. Keep imports to what the
engine/tools layer exposes.
"""
import pytest

from friday_agent.api.provider import (
    AssistantResponse,
    StopReason,
    TextBlock,
    TokenUsage,
    ToolUseBlock,
)
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState
from friday_agent.messages.types import create_user_message
from friday_agent.tools.builtin import builtin_tools
from friday_agent.tools.builtin.todo_write import TodoWrite
from friday_agent.tools.builtin.example_tool import ExampleTool
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider


def test_builtin_tools_includes_todo_write():
    tools = builtin_tools()
    assert any(isinstance(t, TodoWrite) for t in tools)
    assert any(t.name == "TodoWrite" for t in tools)


@pytest.mark.asyncio
async def test_step_registers_todo_write_without_caller_injection():
    """tools=[] still exposes TodoWrite to the provider (auto-registered)."""
    end = AssistantResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    fake = FakeLLMProvider(responses=[end])
    engine = FridayAgent(provider=fake, tools=[])
    await collect_turn(engine, LoopState(messages=[create_user_message("hi")]))
    names = {t["name"] for t in fake.received_tools[0]}
    assert "TodoWrite" in names


@pytest.mark.asyncio
async def test_step_keeps_caller_tools_and_adds_builtins():
    """Caller tools are preserved AND built-ins are added."""
    end = AssistantResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    fake = FakeLLMProvider(responses=[end])
    engine = FridayAgent(provider=fake, tools=[ExampleTool()])
    await collect_turn(engine, LoopState(messages=[create_user_message("hi")]))
    names = {t["name"] for t in fake.received_tools[0]}
    assert {"ExampleTool", "TodoWrite"} <= names


def test_caller_injecting_builtin_name_raises():
    """Passing a tool whose name collides with a built-in is rejected at construction."""
    with pytest.raises(ValueError, match="TodoWrite"):
        FridayAgent(provider=FakeLLMProvider(responses=[]), tools=[TodoWrite()])


@pytest.mark.asyncio
async def test_todo_write_callable_with_zero_caller_wiring():
    """No caller tools at all: the model can still call TodoWrite and LoopState.todos updates."""
    tool_use = AssistantResponse(
        content=[ToolUseBlock(
            id="t1", name="TodoWrite",
            input={"todos": [{"content": "A", "status": "in_progress"}]},
        )],
        stop_reason=StopReason.TOOL_USE, usage=TokenUsage(),
    )
    end = AssistantResponse(content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    engine = FridayAgent(provider=FakeLLMProvider(responses=[tool_use, end]), tools=[])

    _, outcome = await collect_turn(engine, LoopState(messages=[create_user_message("plan it")]))

    assert isinstance(outcome, LoopState)
    assert outcome.todos == [{"content": "A", "status": "in_progress"}]
