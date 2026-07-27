"""core/loop.py turn-local reminder generalization — with_turn_reminders +
run_one_turn(turn_reminders=...).

Cache invariant: per-turn mutable text (todo reminder, caller-supplied
reminders) rides messages[-1] of the API view only and never lands in the
persisted LoopState — messages[-2] and earlier must stay byte-stable for the
prompt cache to keep hitting (see anthropic_provider._apply_cache_control).
"""
import pytest

from friday_agent.api.provider import AssistantResponse, StopReason, TextBlock, TokenUsage
from friday_agent.core.loop import run_one_turn, with_turn_reminders
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import ContentBlock, Message, create_user_message
from tests.fakes import FakeLLMProvider


def _user(text: str) -> Message:
    return create_user_message(text)


def _assistant(text: str) -> Message:
    return Message(
        type="assistant", role="assistant",
        content=[ContentBlock(type="text", text=text)],
    )


def _end_turn_provider() -> FakeLLMProvider:
    end = AssistantResponse(
        content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN, usage=TokenUsage()
    )
    return FakeLLMProvider(responses=[end])


async def _drain(agen):
    items = []
    async for item in agen:
        items.append(item)
    return items


# -- with_turn_reminders (unit) ---------------------------------------------

def test_appends_blocks_to_trailing_user_message():
    messages = [_user("hello")]
    out = with_turn_reminders(messages, ["REM-A", "REM-B"])
    assert len(out) == 1
    assert [b.text for b in out[-1].content] == ["hello", "REM-A", "REM-B"]  # order kept


def test_does_not_mutate_input_messages():
    messages = [_user("hello")]
    with_turn_reminders(messages, ["REM"])
    assert len(messages[-1].content) == 1  # original untouched (turn-local)


def test_empty_texts_are_filtered():
    messages = [_user("hello")]
    out = with_turn_reminders(messages, ["", "REM", ""])
    assert [b.text for b in out[-1].content] == ["hello", "REM"]


def test_all_empty_is_noop():
    messages = [_user("hello")]
    out = with_turn_reminders(messages, ["", ""])
    assert out is messages  # no blocks appended at all


def test_trailing_assistant_gets_new_user_message():
    messages = [_user("hi"), _assistant("ok")]
    out = with_turn_reminders(messages, ["REM"])
    assert len(out) == 3
    assert out[-1].role == "user"
    assert out[-1].content[0].text == "REM"


# -- run_one_turn(turn_reminders=...) (integration) --------------------------

@pytest.mark.asyncio
async def test_turn_reminders_injected_into_last_api_message():
    provider = _end_turn_provider()
    state = LoopState(messages=[_user("hi")])

    await _drain(run_one_turn(
        provider=provider, tools=[], tool_schemas=[], state=state,
        turn_reminders=["EXTRA-REMINDER"],
    ))

    last = provider.received_messages[0][-1]
    assert last["role"] == "user"
    joined = "".join(b.get("text", "") for b in last["content"])
    assert "EXTRA-REMINDER" in joined


@pytest.mark.asyncio
async def test_turn_reminders_after_todo_reminder():
    provider = _end_turn_provider()
    state = LoopState(
        messages=[_user("hi")],
        todos=[{"content": "collect", "status": "in_progress"}],
    )

    await _drain(run_one_turn(
        provider=provider, tools=[], tool_schemas=[], state=state,
        turn_reminders=["EXTRA-REMINDER"],
    ))

    joined = "".join(b.get("text", "") for b in provider.received_messages[0][-1]["content"])
    assert joined.index("Current todo list") < joined.index("EXTRA-REMINDER")


@pytest.mark.asyncio
async def test_reminder_never_persisted_in_loop_state():
    provider = _end_turn_provider()
    state = LoopState(messages=[_user("hi")])

    items = await _drain(run_one_turn(
        provider=provider, tools=[], tool_schemas=[], state=state,
        turn_reminders=["EXTRA-REMINDER"],
    ))

    assert isinstance(items[-1], Terminal)
    assert len(state.messages[-1].content) == 1  # original state untouched
    assert state.messages[-1].content[0].text == "hi"


@pytest.mark.asyncio
async def test_no_turn_reminders_default_unchanged():
    # turn_reminders omitted -> API view identical to the pre-generalization one.
    provider = _end_turn_provider()
    state = LoopState(messages=[_user("hi")])

    await _drain(run_one_turn(
        provider=provider, tools=[], tool_schemas=[], state=state,
    ))

    last = provider.received_messages[0][-1]
    assert [b.get("text") for b in last["content"]] == ["hi"]
