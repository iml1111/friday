"""engine.compact(reuse_prefix=True) — the summary call shares step()'s system prompt and tools."""
import pytest

from friday_agent.api.provider import AssistantResponse, StopReason, TextBlock, TokenUsage, ToolUseBlock
from friday_agent.context.compact import COMPACT_PROMPT, SUMMARIZER_SYSTEM_PROMPT, compact_conversation
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState
from friday_agent.memory.store import MemoryEntry, MemoryType
from friday_agent.messages.types import create_user_message
from friday_agent.tools.builtin.example_tool import ExampleTool
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider, InMemoryStore


def _text(text: str) -> AssistantResponse:
    return AssistantResponse(content=[TextBlock(text=text)], stop_reason=StopReason.END_TURN, usage=TokenUsage())


def _summary(body: str = "S") -> AssistantResponse:
    return _text(f"<analysis>a</analysis><summary>{body}</summary>")


async def _engine_after_one_turn(fake: FakeLLMProvider, **kwargs):
    engine = FridayAgent(provider=fake, tools=[ExampleTool()], system_prompt="DOMAIN", **kwargs)
    _, outcome = await collect_turn(engine, LoopState(messages=[create_user_message("hi")]))
    return engine, outcome.state


def _summary_text(state: LoopState) -> str:
    return state.messages[0].content[0].text


@pytest.mark.asyncio
async def test_reuse_prefix_matches_step_system_and_tools():
    fake = FakeLLMProvider(responses=[_text("hello"), _summary()])
    engine, state = await _engine_after_one_turn(fake)

    await engine.compact(state, reuse_prefix=True)

    assert fake.received_system_prompts[1] == fake.received_system_prompts[0]
    assert fake.received_tools[1] == fake.received_tools[0]
    assert fake.received_messages[1][-1]["content"] == COMPACT_PROMPT


@pytest.mark.asyncio
async def test_reuse_prefix_matches_step_with_memory_mounted():
    store = InMemoryStore()
    await store.save(MemoryEntry(name="n", description="d", type=MemoryType.user, body="b"))
    fake = FakeLLMProvider(responses=[_text("hello"), _summary()])
    engine, state = await _engine_after_one_turn(fake, memory=store)

    await engine.compact(state, reuse_prefix=True)

    assert fake.received_system_prompts[1] == fake.received_system_prompts[0]
    assert fake.received_tools[1] == fake.received_tools[0]


@pytest.mark.asyncio
async def test_tool_use_reply_retries_once_without_tools():
    sneaky = AssistantResponse(
        content=[ToolUseBlock(id="x", name="ExampleTool", input={"payload": "p"})],
        stop_reason=StopReason.TOOL_USE,
        usage=TokenUsage(),
    )
    fake = FakeLLMProvider(responses=[_text("hello"), sneaky, _summary("FROM RETRY")])
    engine, state = await _engine_after_one_turn(fake)

    compacted = await engine.compact(state, reuse_prefix=True)

    assert fake.call_count == 3
    assert fake.received_tools[2] == []
    assert fake.received_system_prompts[2] == fake.received_system_prompts[0]
    assert "FROM RETRY" in _summary_text(compacted)


@pytest.mark.asyncio
async def test_missing_summary_retries_once_without_tools():
    fake = FakeLLMProvider(responses=[_text("hello"), _text("no tags at all"), _summary("SECOND")])
    engine, state = await _engine_after_one_turn(fake)

    compacted = await engine.compact(state, reuse_prefix=True)

    assert fake.call_count == 3
    assert "SECOND" in _summary_text(compacted)


@pytest.mark.asyncio
async def test_default_compact_is_unchanged():
    fake = FakeLLMProvider(responses=[_text("hello"), _summary()])
    engine, state = await _engine_after_one_turn(fake)

    await engine.compact(state)

    assert fake.received_system_prompts[1] == SUMMARIZER_SYSTEM_PROMPT
    assert fake.received_tools[1] == []
    assert fake.call_count == 2


@pytest.mark.asyncio
async def test_compact_conversation_default_path_never_retries():
    fake = FakeLLMProvider(responses=[_text("no tags at all")])

    result = await compact_conversation(provider=fake, messages=[{"role": "user", "content": "x"}])

    assert result == "no tags at all"
    assert fake.call_count == 1


@pytest.mark.asyncio
async def test_compact_never_renders_turn_sections():
    rendered: list[int] = []

    async def section(state: LoopState) -> str:
        rendered.append(1)
        return "PER-TURN"

    fake = FakeLLMProvider(responses=[_text("hello"), _summary(), _summary()])
    engine, state = await _engine_after_one_turn(fake, turn_sections=[section])
    rendered.clear()

    await engine.compact(state)
    await engine.compact(state, reuse_prefix=True)

    assert rendered == []
    for call in (1, 2):
        assert "PER-TURN" not in str(fake.received_messages[call])
