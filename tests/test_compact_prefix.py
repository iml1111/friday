"""engine.compact() — the summary call always shares step()'s system prompt, tools and config."""
import pytest

from friday_agent.api.provider import AssistantResponse, StopReason, TextBlock, TokenUsage, ToolUseBlock
from friday_agent.context.compact import COMPACT_PROMPT
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState
from friday_agent.memory.store import MemoryEntry, MemoryType
from friday_agent.messages.types import create_user_message
from friday_agent.tools.builtin.example_tool import ExampleTool
from tests._drive import collect_turn
from tests.fakes import FakeConfig, FakeLLMProvider, InMemoryStore


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
async def test_compact_matches_step_system_and_tools():
    fake = FakeLLMProvider(responses=[_text("hello"), _summary()])
    engine, state = await _engine_after_one_turn(fake)

    await engine.compact(state)

    assert fake.received_system_prompts[1] == fake.received_system_prompts[0]
    assert fake.received_tools[1] == fake.received_tools[0]
    assert fake.received_messages[1][-1]["content"] == COMPACT_PROMPT
    assert fake.call_count == 2


@pytest.mark.asyncio
async def test_compact_matches_step_with_memory_mounted():
    store = InMemoryStore()
    await store.save(MemoryEntry(name="n", description="d", type=MemoryType.user, body="b"))
    fake = FakeLLMProvider(responses=[_text("hello"), _summary()])
    engine, state = await _engine_after_one_turn(fake, memory=store)

    await engine.compact(state)

    assert fake.received_system_prompts[1] == fake.received_system_prompts[0]
    assert fake.received_tools[1] == fake.received_tools[0]


@pytest.mark.asyncio
async def test_summary_call_inherits_agent_config_with_summary_output_budget():
    """Same config as step() (e.g. thinking settings, which the message cache keys on);
    only max_tokens is raised to the summary budget."""
    fake = FakeLLMProvider(responses=[_text("hello"), _summary()])
    agent_config = FakeConfig(max_tokens=1000, temperature=0.3)
    engine, state = await _engine_after_one_turn(fake, config=agent_config)

    await engine.compact(state)

    sent = fake.received_configs[1]
    assert sent.temperature == 0.3
    assert sent.max_tokens == 20_000
    assert agent_config.max_tokens == 1000  # the agent's own config is untouched


@pytest.mark.asyncio
async def test_summary_call_keeps_a_larger_agent_max_tokens():
    fake = FakeLLMProvider(responses=[_text("hello"), _summary()])
    engine, state = await _engine_after_one_turn(fake, config=FakeConfig(max_tokens=64000))

    await engine.compact(state)

    assert fake.received_configs[1].max_tokens == 64000


@pytest.mark.asyncio
async def test_tool_use_reply_retries_once_without_tools():
    sneaky = AssistantResponse(
        content=[ToolUseBlock(id="x", name="ExampleTool", input={"payload": "p"})],
        stop_reason=StopReason.TOOL_USE,
        usage=TokenUsage(),
    )
    fake = FakeLLMProvider(responses=[_text("hello"), sneaky, _summary("FROM RETRY")])
    engine, state = await _engine_after_one_turn(fake)

    compacted = await engine.compact(state)

    assert fake.call_count == 3
    assert fake.received_tools[2] == []
    assert fake.received_system_prompts[2] == fake.received_system_prompts[0]
    assert "FROM RETRY" in _summary_text(compacted)


@pytest.mark.asyncio
async def test_missing_summary_retries_once_without_tools():
    fake = FakeLLMProvider(responses=[_text("hello"), _text("no tags at all"), _summary("SECOND")])
    engine, state = await _engine_after_one_turn(fake)

    compacted = await engine.compact(state)

    assert fake.call_count == 3
    assert "SECOND" in _summary_text(compacted)


@pytest.mark.asyncio
async def test_compact_never_renders_turn_sections():
    rendered: list[int] = []

    async def section(state: LoopState) -> str:
        rendered.append(1)
        return "PER-TURN"

    fake = FakeLLMProvider(responses=[_text("hello"), _summary()])
    engine, state = await _engine_after_one_turn(fake, turn_sections=[section])
    rendered.clear()

    await engine.compact(state)

    assert rendered == []
    assert "PER-TURN" not in str(fake.received_messages[1])
