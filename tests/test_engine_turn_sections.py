"""engine.step(state, turn_sections=[...]) — per-turn sections ride messages[-1] only."""
import pytest

from friday_agent.api.provider import AssistantResponse, StopReason, TextBlock, TokenUsage, ToolUseBlock
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState
from friday_agent.memory.store import MemoryEntry, MemoryType
from friday_agent.messages.types import SYSTEM_REMINDER_PREFIX, create_user_message
from friday_agent.tools.builtin.example_tool import ExampleTool
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider, InMemoryStore


def _text(text: str = "done") -> AssistantResponse:
    return AssistantResponse(content=[TextBlock(text=text)], stop_reason=StopReason.END_TURN, usage=TokenUsage())


def _tool_call(tool_id: str) -> AssistantResponse:
    return AssistantResponse(
        content=[ToolUseBlock(id=tool_id, name="ExampleTool", input={"payload": "x"})],
        stop_reason=StopReason.TOOL_USE,
        usage=TokenUsage(),
    )


def _texts(api_message: dict) -> list[str]:
    return [b.get("text", "") for b in api_message["content"] if b.get("type") == "text"]


@pytest.mark.asyncio
async def test_section_rides_last_user_message_wrapped_as_reminder():
    fake = FakeLLMProvider(responses=[_text()])

    await collect_turn(FridayAgent(provider=fake), LoopState(messages=[create_user_message("hi")]), ["SECTION"])

    last = fake.received_messages[0][-1]
    assert last["role"] == "user"
    texts = _texts(last)
    assert texts[0] == "hi"
    assert texts[1].startswith(SYSTEM_REMINDER_PREFIX)
    assert "SECTION" in texts[1]


@pytest.mark.asyncio
async def test_section_never_persisted():
    fake = FakeLLMProvider(responses=[_tool_call("t1")])
    engine = FridayAgent(provider=fake, tools=[ExampleTool()])

    _, outcome = await collect_turn(engine, LoopState(messages=[create_user_message("hi")]), ["SECTION"])

    assert isinstance(outcome, LoopState)
    assert "SECTION" not in str(outcome.to_dict())


@pytest.mark.asyncio
async def test_without_sections_request_is_unchanged():
    default = FakeLLMProvider(responses=[_text()])
    empty = FakeLLMProvider(responses=[_text()])
    state = LoopState(messages=[create_user_message("hi")])

    await collect_turn(FridayAgent(provider=default), state)
    await collect_turn(FridayAgent(provider=empty), state, [])

    assert default.received_messages[0] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert empty.received_messages[0] == default.received_messages[0]


@pytest.mark.asyncio
async def test_empty_section_is_dropped():
    fake = FakeLLMProvider(responses=[_text()])

    await collect_turn(FridayAgent(provider=fake), LoopState(messages=[create_user_message("hi")]), [""])

    assert _texts(fake.received_messages[0][-1]) == ["hi"]


@pytest.mark.asyncio
async def test_order_todo_then_memory_then_sections():
    store = InMemoryStore()
    await store.save(MemoryEntry(name="pref", description="likes tea", type=MemoryType.user, body="tea"))

    fake = FakeLLMProvider(responses=[_text()])
    engine = FridayAgent(provider=fake, memory=store)
    state = LoopState(messages=[create_user_message("hi")], todos=[{"content": "a", "status": "pending"}])

    await collect_turn(engine, state, ["FIRST", "SECOND"])

    joined = "\n".join(_texts(fake.received_messages[0][-1]))
    assert (
        joined.index("Current todo list")
        < joined.index("likes tea")
        < joined.index("FIRST")
        < joined.index("SECOND")
    )


@pytest.mark.asyncio
async def test_sections_are_chosen_per_call():
    """Each step() call carries only the sections passed to it — the caller can
    include a section on one call and omit it on the next."""
    fake = FakeLLMProvider(responses=[_tool_call("t1"), _text()])
    engine = FridayAgent(provider=fake, tools=[ExampleTool()])

    _, state = await collect_turn(engine, LoopState(messages=[create_user_message("hi")]), ["ONLY-FIRST"])
    await collect_turn(engine, state)

    first, second = fake.received_messages
    assert "ONLY-FIRST" in str(first[-1])
    assert "ONLY-FIRST" not in str(second)


@pytest.mark.asyncio
async def test_cache_prefix_stable_across_turns():
    """Everything before messages[-1] is byte-identical turn to turn."""
    fake = FakeLLMProvider(responses=[_tool_call("t1"), _tool_call("t2"), _text()])
    engine = FridayAgent(provider=fake, tools=[ExampleTool()])

    state = LoopState(messages=[create_user_message("hi")])
    _, state = await collect_turn(engine, state, ["tick 1"])
    _, state = await collect_turn(engine, state, ["tick 2"])
    await collect_turn(engine, state, ["tick 3"])

    _, second, third = fake.received_messages
    assert third[: len(second) - 1] == second[:-1]  # messages[-2] and earlier unchanged
    assert "tick 2" not in str(third)  # last turn's section did not persist
    assert "tick 3" in str(third[-1])
