"""Todo/Task tracking: state effects, reminder injection, distributed resume."""
from friday_agent.core.loop import apply_state_effects, render_todo_reminder, with_todo_reminder
from friday_agent.messages.types import ContentBlock, create_user_message, create_tool_result_message

import json
import pytest

from friday_agent.api.provider import (
    AssistantResponse, StopReason, TextBlock, ToolUseBlock, TokenUsage,
)
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState, Terminal
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider
from friday_agent.tools.builtin.todo_write import TodoWrite
from friday_agent.tools.builtin.example_tool import ExampleTool


def _api_text(api_messages: list[dict]) -> str:
    """Concatenate every text block across a normalized API message list."""
    parts = []
    for m in api_messages:
        for b in m["content"]:
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
    return "\n".join(parts)


# --- apply_state_effects -----------------------------------------------------

def test_apply_state_effects_replaces_on_todos_effect():
    start = [{"content": "old", "status": "pending"}]
    new = [{"content": "new", "status": "in_progress"}]
    assert apply_state_effects(start, [{"todos": new}]) == new


def test_apply_state_effects_carry_forward_when_no_effect():
    start = [{"content": "keep", "status": "pending"}]
    assert apply_state_effects(start, []) == start


def test_apply_state_effects_last_write_wins():
    a = [{"content": "a", "status": "pending"}]
    b = [{"content": "b", "status": "completed"}]
    assert apply_state_effects([], [{"todos": a}, {"todos": b}]) == b


def test_apply_state_effects_ignores_unknown_effect_keys():
    start = [{"content": "keep", "status": "pending"}]
    assert apply_state_effects(start, [{"something_else": 1}]) == start


# --- render_todo_reminder ----------------------------------------------------

def test_render_todo_reminder_empty_is_blank():
    assert render_todo_reminder([]) == ""


def test_render_todo_reminder_formats_items():
    out = render_todo_reminder([
        {"content": "Wire it up", "status": "in_progress"},
        {"content": "Add tests", "status": "pending"},
    ])
    assert "<system-reminder>" in out and "</system-reminder>" in out
    assert "- [in_progress] Wire it up" in out
    assert "- [pending] Add tests" in out


# --- with_todo_reminder ------------------------------------------------------

def test_with_todo_reminder_joins_trailing_user_turn():
    todos = [{"content": "A", "status": "in_progress"}]
    original = [create_tool_result_message(tool_use_id="t1", result_text="ok")]  # role == "user"
    out = with_todo_reminder(original, todos)
    # No new consecutive user message: still one trailing user turn.
    assert len(out) == len(original)
    last = out[-1]
    assert last.role == "user"
    assert last.content[0].type == "tool_result"          # original block preserved, first
    assert last.content[-1].type == "text"                # reminder appended after
    assert "<system-reminder>" in last.content[-1].text
    # Input is not mutated (distributed-safe).
    assert len(original[-1].content) == 1


def test_with_todo_reminder_empty_todos_returns_unchanged():
    msgs = [create_user_message("hi")]
    assert with_todo_reminder(msgs, []) is msgs


def test_with_todo_reminder_appends_user_msg_when_trailing_not_user():
    todos = [{"content": "A", "status": "pending"}]
    assistant = create_user_message("x")
    assistant.role = "assistant"  # simulate a trailing non-user turn (defensive path)
    out = with_todo_reminder([assistant], todos)
    assert len(out) == 2 and out[-1].role == "user"
    assert "<system-reminder>" in out[-1].content[0].text


# --- TodoWrite tool ----------------------------------------------------------

@pytest.mark.asyncio
async def test_todowrite_returns_todos_state_effect():
    tool = TodoWrite()
    args = {"todos": [
        {"content": "A", "status": "in_progress"},
        {"content": "B", "status": "pending"},
    ]}
    result = await tool.call(args)
    assert result.is_error is False
    assert result.state_effect == {"todos": args["todos"]}
    assert "2" in str(result.data)


def test_todowrite_is_not_concurrency_safe():
    assert TodoWrite().is_concurrency_safe({}) is False


@pytest.mark.asyncio
async def test_todowrite_rejects_bad_status():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        await TodoWrite().call({"todos": [{"content": "A", "status": "not-a-status"}]})


# --- orchestrator effects_sink -----------------------------------------------

@pytest.mark.asyncio
async def test_run_tools_collects_state_effects_into_sink():
    from friday_agent.tools.orchestrator import run_tools
    blocks = [ContentBlock(type="tool_use", id="t1", name="TodoWrite",
                           input={"todos": [{"content": "A", "status": "pending"}]})]
    sink: list[dict] = []
    msgs = [m async for m in run_tools(blocks, [TodoWrite()], effects_sink=sink)]
    assert len(msgs) == 1 and msgs[0].content[0].type == "tool_result"
    assert sink == [{"todos": [{"content": "A", "status": "pending"}]}]


@pytest.mark.asyncio
async def test_run_tools_without_sink_still_yields_messages():
    from friday_agent.tools.orchestrator import run_tools
    blocks = [ContentBlock(type="tool_use", id="t1", name="ExampleTool",
                           input={"payload": "x", "mutating": False})]
    msgs = [m async for m in run_tools(blocks, [ExampleTool()])]
    assert len(msgs) == 1 and msgs[0].content[0].type == "tool_result"


# --- loop integration --------------------------------------------------------

@pytest.mark.asyncio
async def test_todowrite_updates_next_state_and_reminder_appears_next_turn():
    todos = [{"content": "Wire it up", "status": "in_progress"},
             {"content": "Add tests", "status": "pending"}]
    r1 = AssistantResponse(
        content=[ToolUseBlock(id="t1", name="TodoWrite", input={"todos": todos})],
        stop_reason=StopReason.TOOL_USE, usage=TokenUsage(),
    )
    r2 = AssistantResponse(content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    fake = FakeLLMProvider(responses=[r1, r2])
    engine = FridayAgent(provider=fake, tools=[])

    # Turn 1: todos empty at turn start -> NO reminder; effect lands in next state.
    _, outcome1 = await collect_turn(engine, LoopState(messages=[create_user_message("do multi-step work")]))
    assert isinstance(outcome1, LoopState)
    assert outcome1.todos == todos
    assert "<system-reminder>" not in _api_text(fake.received_messages[0])

    # Turn 2: reminder reflects committed todos, joined onto the trailing user (tool_result) turn.
    _, outcome2 = await collect_turn(engine, outcome1)
    assert isinstance(outcome2, Terminal) and outcome2.reason == "completed"
    turn2 = _api_text(fake.received_messages[1])
    assert "<system-reminder>" in turn2
    assert "[in_progress] Wire it up" in turn2


@pytest.mark.asyncio
async def test_reminder_never_leaks_into_persisted_state():
    preset = [{"content": "X", "status": "in_progress"}]
    r1 = AssistantResponse(
        content=[ToolUseBlock(id="t1", name="ExampleTool", input={"payload": "p", "mutating": False})],
        stop_reason=StopReason.TOOL_USE, usage=TokenUsage(),
    )
    fake = FakeLLMProvider(responses=[r1])
    engine = FridayAgent(provider=fake, tools=[ExampleTool()])

    _, outcome = await collect_turn(engine, LoopState(messages=[create_user_message("go")], todos=preset))
    assert isinstance(outcome, LoopState)
    # The API saw the reminder...
    assert "<system-reminder>" in _api_text(fake.received_messages[0])
    # ...but the persisted next state did NOT (built from clean state_messages).
    persisted = " ".join(b.text or "" for m in outcome.messages for b in m.content if b.type == "text")
    assert "<system-reminder>" not in persisted
    # ExampleTool emits no effect -> todos carried forward unchanged.
    assert outcome.todos == preset


# --- distributed resume ------------------------------------------------------

@pytest.mark.asyncio
async def test_todos_survive_serialize_roundtrip_and_reminder_regenerates():
    todos = [{"content": "A", "status": "in_progress"}]
    r1 = AssistantResponse(
        content=[ToolUseBlock(id="t1", name="TodoWrite", input={"todos": todos})],
        stop_reason=StopReason.TOOL_USE, usage=TokenUsage(),
    )
    r2 = AssistantResponse(content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    fake = FakeLLMProvider(responses=[r1, r2])
    engine = FridayAgent(provider=fake, tools=[])

    # Worker A: run the TodoWrite turn, then serialize the resulting state.
    _, outcome1 = await collect_turn(engine, LoopState(messages=[create_user_message("go")]))
    assert isinstance(outcome1, LoopState)
    blob = json.dumps(outcome1.to_dict(), ensure_ascii=False)

    # Worker B: restore -> todos preserved -> resume regenerates the same reminder.
    restored = LoopState.from_dict(json.loads(blob))
    assert restored.todos == todos
    _, outcome2 = await collect_turn(engine, restored)
    assert isinstance(outcome2, Terminal) and outcome2.reason == "completed"
    assert "<system-reminder>" in _api_text(fake.received_messages[1])
    assert "[in_progress] A" in _api_text(fake.received_messages[1])
