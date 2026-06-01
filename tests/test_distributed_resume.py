"""Distributed resume simulation — step via JSON serialization matches an in-process drive."""
import json

import pytest

from friday_agent.api.provider import (
    AssistantResponse, ContextOverflowError, StopReason, TextBlock, ToolUseBlock, TokenUsage,
)
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import create_user_message
from friday_agent.tools.builtin.example_tool import ExampleTool
from tests._drive import drive, collect_turn
from tests.fakes import FakeLLMProvider


def _scripted_responses():
    """One tool_use then end_turn. New instance per provider keeps deques independent."""
    return [
        AssistantResponse(
            content=[ToolUseBlock(id="tu1", name="ExampleTool", input={"payload": "x", "mutating": False})],
            stop_reason=StopReason.TOOL_USE, usage=TokenUsage(),
        ),
        AssistantResponse(content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN, usage=TokenUsage()),
    ]


def _sig(messages):
    """UUIDs differ per run, so compare by content signature."""
    return [
        (m.type, m.role, m.is_compact_summary,
         [(b.type, b.text, b.name, b.input, b.tool_use_id, b.content, b.is_error) for b in m.content])
        for m in messages
    ]


@pytest.mark.asyncio
async def test_distributed_resume_matches_full_run():
    # Baseline: in-process drive (loops step to completion).
    ref_messages = []
    ref_terminal = None
    async for item in drive(
        provider=FakeLLMProvider(responses=_scripted_responses()),
        tools=[ExampleTool()],
        messages=[create_user_message("run example")],
    ):
        if isinstance(item, Terminal):
            ref_terminal = item
        else:
            ref_messages.append(item)
    assert ref_terminal.reason == "completed"

    # Distributed: step → (serialize → deserialize) → step loop.
    dist_engine = FridayAgent(provider=FakeLLMProvider(responses=_scripted_responses()), tools=[ExampleTool()])
    collected = []
    msgs, outcome = await collect_turn(dist_engine, LoopState(messages=[create_user_message("run example")]))
    collected.extend(msgs)
    hops = 0
    while isinstance(outcome, LoopState):
        pre_state_sig = _sig(outcome.messages)
        blob = json.dumps(outcome.to_dict(), ensure_ascii=False)
        restored = LoopState.from_dict(json.loads(blob))
        assert _sig(restored.messages) == pre_state_sig
        msgs, outcome = await collect_turn(dist_engine, restored)
        collected.extend(msgs)
        hops += 1
        assert hops < 10, "resume did not converge (infinite loop guard)"

    assert outcome.reason == "completed"
    assert _sig(collected) == _sig(ref_messages)


@pytest.mark.asyncio
async def test_loopstate_has_no_dangling_tool_use():
    """Every tool_use in LoopState.messages has a matching tool_result (pairing invariant)."""
    engine = FridayAgent(provider=FakeLLMProvider(responses=_scripted_responses()), tools=[ExampleTool()])
    _, outcome = await collect_turn(engine, LoopState(messages=[create_user_message("run example")]))
    assert isinstance(outcome, LoopState)
    msgs = outcome.messages
    tool_use_ids = {b.id for m in msgs for b in m.content if b.type == "tool_use"}
    tool_result_ids = {b.tool_use_id for m in msgs for b in m.content if b.type == "tool_result"}
    assert tool_use_ids <= tool_result_ids, "dangling tool_use (no matching tool_result)"


@pytest.mark.asyncio
async def test_distributed_overflow_recovers_across_serialize_boundary():
    """Overflow → compact → serialize state → restore → step completes."""
    fake = FakeLLMProvider(
        responses=[
            AssistantResponse(
                content=[TextBlock(text="<summary>DIST-RECOVERED</summary>")],
                stop_reason=StopReason.END_TURN, usage=TokenUsage(),
            ),
            AssistantResponse(content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN, usage=TokenUsage()),
        ],
        errors={0: ContextOverflowError("prompt too long")},
    )
    engine = FridayAgent(provider=fake, tools=[])
    state = LoopState(messages=[create_user_message("long conversation")])

    # Worker A: step overflows → compact → serialize the compacted state.
    with pytest.raises(ContextOverflowError):
        async for _ in engine.step(state):
            pass
    compacted = await engine.compact(state)
    blob = json.dumps(compacted.to_dict(), ensure_ascii=False)

    # Worker B: restore and resume to completion.
    restored = LoopState.from_dict(json.loads(blob))
    _, outcome = await collect_turn(engine, restored)
    assert isinstance(outcome, Terminal)
    assert outcome.reason == "completed"
    assert fake.call_count == 3  # overflow(0) + compact summary(1) + resumed step(2)
