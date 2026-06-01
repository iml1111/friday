"""FridayAgent entry-point tests.

Verifies the step() API and the tests/_drive.py driver:
  - drive() runs a conversation to completion (Terminal reason="completed").
  - system_prompt is forwarded to provider.complete().
  - drive() collects emitted messages.
  - provider is a required argument.
  - step() continue-and-resume flow across turns.
"""
import pytest

from friday_agent.api.prompts import GENERAL_AGENT_GUIDANCE
from friday_agent.context.compact import SUMMARIZER_SYSTEM_PROMPT
from friday_agent.api.provider import AssistantResponse, StopReason, TextBlock, TokenUsage, ToolUseBlock
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import create_user_message
from friday_agent.tools.builtin.example_tool import ExampleTool
from tests._drive import drive, collect_turn
from tests.fakes import FakeLLMProvider


@pytest.mark.asyncio
async def test_drive_runs_to_completion():
    """Single end_turn response: Terminal(reason="completed") and assistant "hello" text collected."""
    end = AssistantResponse(
        content=[TextBlock(text="hello")],
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(),
    )
    fake = FakeLLMProvider(responses=[end])

    messages = []
    terminal = None
    async for item in drive(provider=fake, tools=[ExampleTool()], messages=[create_user_message("hi")]):
        if isinstance(item, Terminal):
            terminal = item
        else:
            messages.append(item)

    assert isinstance(terminal, Terminal)
    assert terminal.reason == "completed"
    assistant_msgs = [m for m in messages if m.type == "assistant"]
    assert any(
        block.text == "hello"
        for m in assistant_msgs
        for block in m.content
        if block.text is not None
    )


@pytest.mark.asyncio
async def test_drive_passes_system_prompt():
    """system_prompt is forwarded to provider.complete()."""
    end = AssistantResponse(
        content=[TextBlock(text="ok")],
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(),
    )
    fake = FakeLLMProvider(responses=[end])

    terminal = None
    async for item in drive(provider=fake, tools=[], messages=[create_user_message("ping")], system_prompt="You are a test assistant."):
        if isinstance(item, Terminal):
            terminal = item

    assert terminal.reason == "completed"
    assert fake.received_system_prompts[0].startswith("You are a test assistant.")
    assert GENERAL_AGENT_GUIDANCE in fake.received_system_prompts[0]


@pytest.mark.asyncio
async def test_drive_returns_collected_messages():
    """drive yields at least one message plus a completed Terminal."""
    end = AssistantResponse(
        content=[TextBlock(text="response text")],
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(),
    )
    fake = FakeLLMProvider(responses=[end])

    messages = []
    terminal = None
    async for item in drive(provider=fake, tools=[], messages=[create_user_message("hello")]):
        if isinstance(item, Terminal):
            terminal = item
        else:
            messages.append(item)

    assert len(messages) > 0
    assert terminal.reason == "completed"


# ---------------------------------------------------------------------------
# provider is required
# ---------------------------------------------------------------------------

def test_provider_is_required():
    """provider is a required argument; omitting it raises TypeError."""
    with pytest.raises(TypeError):
        FridayAgent(tools=[])


@pytest.mark.asyncio
async def test_step_single_turn_completed():
    end = AssistantResponse(content=[TextBlock(text="hello")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    engine = FridayAgent(provider=FakeLLMProvider(responses=[end]), tools=[])

    msgs, outcome = await collect_turn(engine, LoopState(messages=[create_user_message("hi")]))

    assert isinstance(outcome, Terminal)
    assert outcome.reason == "completed"
    assert any(b.type == "text" and b.text == "hello" for m in msgs for b in m.content)


@pytest.mark.asyncio
async def test_step_resumes_to_completion():
    """step() yields a LoopState after tool_use; a second step() resumes and completes."""
    tool_use = AssistantResponse(
        content=[ToolUseBlock(id="tu1", name="ExampleTool", input={"payload": "x", "mutating": False})],
        stop_reason=StopReason.TOOL_USE, usage=TokenUsage(),
    )
    end = AssistantResponse(content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    engine = FridayAgent(provider=FakeLLMProvider(responses=[tool_use, end]), tools=[ExampleTool()])

    _, outcome1 = await collect_turn(engine, LoopState(messages=[create_user_message("run example")]))
    assert isinstance(outcome1, LoopState)
    assert outcome1.turn_count == 2

    msgs2, outcome2 = await collect_turn(engine, outcome1)
    assert isinstance(outcome2, Terminal)
    assert outcome2.reason == "completed"
    assert any(b.type == "text" and b.text == "done" for m in msgs2 for b in m.content)


@pytest.mark.asyncio
async def test_compact_summarizes_to_single_message():
    """engine.compact() replaces history with one summary message, preserving turn_count."""
    summary = AssistantResponse(
        content=[TextBlock(text="<analysis>x</analysis><summary>COMPACTED-HISTORY</summary>")],
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(),
    )
    engine = FridayAgent(provider=FakeLLMProvider(responses=[summary]), tools=[])
    state = LoopState(
        messages=[create_user_message("m1"), create_user_message("m2"), create_user_message("m3")],
        turn_count=7,
    )

    new_state = await engine.compact(state)

    assert len(new_state.messages) == 1
    assert new_state.messages[0].is_compact_summary is True
    text = " ".join(b.text or "" for b in new_state.messages[0].content if b.text)
    assert "COMPACTED-HISTORY" in text
    assert new_state.turn_count == 7  # preserved


@pytest.mark.asyncio
async def test_compact_falls_back_to_raw_text_without_summary_tags():
    """When the summary response has no <summary> tags, compact() wraps the raw text."""
    raw = AssistantResponse(
        content=[TextBlock(text="plain summary, no tags")],
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(),
    )
    engine = FridayAgent(provider=FakeLLMProvider(responses=[raw]), tools=[])
    new_state = await engine.compact(LoopState(messages=[create_user_message("a")], turn_count=2))
    assert len(new_state.messages) == 1
    assert new_state.messages[0].is_compact_summary is True
    text = " ".join(b.text or "" for b in new_state.messages[0].content if b.text)
    assert "plain summary, no tags" in text
    assert new_state.turn_count == 2


@pytest.mark.asyncio
async def test_compact_does_not_inject_general_guidance():
    """engine.compact() summary call must NOT carry the main-loop general guidance,
    and must use the dedicated summarizer system (not the caller's role)."""
    summary = AssistantResponse(
        content=[TextBlock(text="<summary>S</summary>")],
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(),
    )
    fake = FakeLLMProvider(responses=[summary])
    engine = FridayAgent(provider=fake, tools=[], system_prompt="You are a domain assistant.")
    await engine.compact(LoopState(messages=[create_user_message("a")], turn_count=1))
    assert GENERAL_AGENT_GUIDANCE not in fake.received_system_prompts[0]
    assert fake.received_system_prompts[0] == SUMMARIZER_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_compact_preserves_todos():
    """Compaction happens in long conversations — exactly when todo anti-drift
    matters most — so todos must survive it (summary is prose; todos are state)."""
    summary = AssistantResponse(
        content=[TextBlock(text="<summary>S</summary>")],
        stop_reason=StopReason.END_TURN, usage=TokenUsage(),
    )
    engine = FridayAgent(provider=FakeLLMProvider(responses=[summary]), tools=[])
    todos = [{"content": "A", "status": "in_progress"}]
    state = LoopState(messages=[create_user_message("m1"), create_user_message("m2")],
                      turn_count=4, todos=todos)

    new_state = await engine.compact(state)

    assert len(new_state.messages) == 1
    assert new_state.turn_count == 4
    assert new_state.todos == todos
