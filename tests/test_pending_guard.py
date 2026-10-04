"""pending_tool_uses() + PendingToolUseError — no request while a tool_use is unanswered."""
import pytest

from friday_agent.api.provider import LLMError
from friday_agent.core.engine import FridayAgent
from friday_agent.core.loop import pending_tool_uses, run_one_turn
from friday_agent.core.state import LoopState, PendingToolUseError
from friday_agent.messages.types import ContentBlock, Message, create_tool_result_message, create_user_message
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider


def _assistant_calls(*ids: str) -> Message:
    return Message(
        type="assistant",
        role="assistant",
        content=[ContentBlock(type="tool_use", id=i, name="f", input={}) for i in ids],
    )


def test_no_assistant_message_means_nothing_pending():
    assert pending_tool_uses(LoopState(messages=[])) == []
    assert pending_tool_uses(LoopState(messages=[create_user_message("hi")])) == []


def test_unanswered_calls_of_last_assistant_are_pending():
    state = LoopState(messages=[
        create_user_message("hi"),
        _assistant_calls("a", "b", "c"),
        create_tool_result_message("b", "ok"),
    ])
    assert [b.id for b in pending_tool_uses(state)] == ["a", "c"]


def test_fully_answered_turn_has_nothing_pending():
    state = LoopState(messages=[
        create_user_message("hi"),
        _assistant_calls("a"),
        create_tool_result_message("a", "ok"),
    ])
    assert pending_tool_uses(state) == []


@pytest.mark.asyncio
async def test_step_rejects_pending_state_before_anything_runs():
    fake = FakeLLMProvider(responses=[])
    state = LoopState(messages=[create_user_message("hi"), _assistant_calls("a", "b")])

    with pytest.raises(PendingToolUseError) as exc:
        await collect_turn(FridayAgent(provider=fake), state, ["S"])

    assert exc.value.tool_use_ids == ["a", "b"]
    assert fake.call_count == 0


@pytest.mark.asyncio
async def test_step_rejects_pending_even_after_a_new_user_message():
    fake = FakeLLMProvider(responses=[])
    state = LoopState(messages=[
        create_user_message("hi"),
        _assistant_calls("a"),
        create_user_message("never mind"),
    ])

    with pytest.raises(PendingToolUseError):
        await collect_turn(FridayAgent(provider=fake), state)
    assert fake.call_count == 0


@pytest.mark.asyncio
async def test_run_one_turn_rejects_pending_state():
    fake = FakeLLMProvider(responses=[])
    state = LoopState(messages=[create_user_message("hi"), _assistant_calls("a")])

    with pytest.raises(PendingToolUseError):
        async for _ in run_one_turn(provider=fake, tools=[], tool_schemas=[], state=state):
            pass
    assert fake.call_count == 0


@pytest.mark.asyncio
async def test_compact_rejects_pending_state():
    fake = FakeLLMProvider(responses=[])
    state = LoopState(messages=[create_user_message("hi"), _assistant_calls("a")])

    with pytest.raises(PendingToolUseError):
        await FridayAgent(provider=fake).compact(state)
    assert fake.call_count == 0


def test_pending_error_is_a_value_error_not_an_llm_error():
    err = PendingToolUseError(["a"])
    assert isinstance(err, ValueError) and not isinstance(err, LLMError)
    assert err.tool_use_ids == ["a"]
    assert "resume()" in str(err)
