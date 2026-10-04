"""FridayAgent(turn_sections=...) — per-turn sections ride messages[-1] only."""
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


async def _turns_section(state: LoopState) -> str:
    return f"SECTION turns={state.turn_count}"


@pytest.mark.asyncio
async def test_section_rides_last_user_message_wrapped_as_reminder():
    fake = FakeLLMProvider(responses=[_text()])
    engine = FridayAgent(provider=fake, turn_sections=[_turns_section])

    await collect_turn(engine, LoopState(messages=[create_user_message("hi")], turn_count=7))

    last = fake.received_messages[0][-1]
    assert last["role"] == "user"
    texts = _texts(last)
    assert texts[0] == "hi"
    assert texts[1].startswith(SYSTEM_REMINDER_PREFIX)
    assert "SECTION turns=7" in texts[1]  # the section received the input state


@pytest.mark.asyncio
async def test_section_never_persisted():
    fake = FakeLLMProvider(responses=[_tool_call("t1")])
    engine = FridayAgent(provider=fake, tools=[ExampleTool()], turn_sections=[_turns_section])

    _, outcome = await collect_turn(engine, LoopState(messages=[create_user_message("hi")]))

    assert isinstance(outcome, LoopState)
    assert "SECTION" not in str(outcome.to_dict())


@pytest.mark.asyncio
async def test_without_sections_request_is_unchanged():
    default = FakeLLMProvider(responses=[_text()])
    empty = FakeLLMProvider(responses=[_text()])
    state = LoopState(messages=[create_user_message("hi")])

    await collect_turn(FridayAgent(provider=default), state)
    await collect_turn(FridayAgent(provider=empty, turn_sections=[]), state)

    assert default.received_messages[0] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert empty.received_messages[0] == default.received_messages[0]


@pytest.mark.asyncio
async def test_empty_section_output_is_dropped():
    async def silent(state: LoopState) -> str:
        return ""

    fake = FakeLLMProvider(responses=[_text()])
    await collect_turn(FridayAgent(provider=fake, turn_sections=[silent]), LoopState(messages=[create_user_message("hi")]))

    assert _texts(fake.received_messages[0][-1]) == ["hi"]


@pytest.mark.asyncio
async def test_order_todo_then_memory_then_sections():
    store = InMemoryStore()
    await store.save(MemoryEntry(name="pref", description="likes tea", type=MemoryType.user, body="tea"))

    async def first(state: LoopState) -> str:
        return "FIRST"

    async def second(state: LoopState) -> str:
        return "SECOND"

    fake = FakeLLMProvider(responses=[_text()])
    engine = FridayAgent(provider=fake, memory=store, turn_sections=[first, second])
    state = LoopState(messages=[create_user_message("hi")], todos=[{"content": "a", "status": "pending"}])

    await collect_turn(engine, state)

    joined = "\n".join(_texts(fake.received_messages[0][-1]))
    assert (
        joined.index("Current todo list")
        < joined.index("likes tea")
        < joined.index("FIRST")
        < joined.index("SECOND")
    )


@pytest.mark.asyncio
async def test_cache_prefix_stable_across_turns():
    """Everything before messages[-1] is byte-identical turn to turn."""
    ticks = {"n": 0}

    async def ticker(state: LoopState) -> str:
        ticks["n"] += 1
        return f"tick {ticks['n']}"

    fake = FakeLLMProvider(responses=[_tool_call("t1"), _tool_call("t2"), _text()])
    engine = FridayAgent(provider=fake, tools=[ExampleTool()], turn_sections=[ticker])

    state = LoopState(messages=[create_user_message("hi")])
    _, state = await collect_turn(engine, state)
    _, state = await collect_turn(engine, state)
    await collect_turn(engine, state)

    _, second, third = fake.received_messages
    assert third[: len(second) - 1] == second[:-1]  # messages[-2] and earlier unchanged
    assert "tick 2" not in str(third)  # last turn's section did not persist
    assert "tick 3" in str(third[-1])


@pytest.mark.asyncio
async def test_section_exception_propagates():
    async def broken(state: LoopState) -> str:
        raise RuntimeError("section failed")

    fake = FakeLLMProvider(responses=[_text()])
    with pytest.raises(RuntimeError, match="section failed"):
        await collect_turn(
            FridayAgent(provider=fake, turn_sections=[broken]),
            LoopState(messages=[create_user_message("hi")]),
        )
    assert fake.call_count == 0
