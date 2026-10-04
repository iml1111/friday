"""run_one_turn(turn_reminders=...) — reminders ride messages[-1] of the API view only.

Cache invariant: per-turn mutable text (the todo list, the memory index, the
caller's turn_sections — all rendered by the engine) rides messages[-1] of the
API view only and never lands in the persisted LoopState — messages[-2] and
earlier must stay byte-stable for the prompt cache to keep hitting (see
anthropic_provider._apply_cache_control).
"""
import pytest

from friday_agent.api.provider import AssistantResponse, StopReason, TextBlock, TokenUsage
from friday_agent.core.loop import run_one_turn
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import ContentBlock, Message, create_tool_result_message, create_user_message
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


async def _sent(state: LoopState, turn_reminders: list[str] | None = None) -> list[dict]:
    """Run one turn; return the API messages the provider received."""
    provider = _end_turn_provider()
    await _drain(run_one_turn(
        provider=provider, tools=[], tool_schemas=[], state=state, turn_reminders=turn_reminders,
    ))
    return provider.received_messages[0]


def _texts(api_message: dict) -> list[str]:
    return [b.get("text") for b in api_message["content"] if b.get("type") == "text"]


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
async def test_reminders_keep_their_order_after_the_original_blocks():
    sent = await _sent(LoopState(messages=[_user("hi")]), ["REM-A", "REM-B"])
    assert len(sent) == 1
    assert _texts(sent[-1]) == ["hi", "REM-A", "REM-B"]


@pytest.mark.asyncio
async def test_empty_reminders_are_dropped():
    sent = await _sent(LoopState(messages=[_user("hi")]), ["", "REM", ""])
    assert _texts(sent[-1]) == ["hi", "REM"]


@pytest.mark.asyncio
async def test_all_empty_reminders_leave_the_request_unchanged():
    assert await _sent(LoopState(messages=[_user("hi")]), ["", ""]) == await _sent(
        LoopState(messages=[_user("hi")])
    )


@pytest.mark.asyncio
async def test_reminders_follow_a_trailing_tool_result():
    # Tool results must precede text in a user turn: the reminder joins the
    # same message, after the tool_result block — no extra user message.
    state = LoopState(messages=[_user("go"), create_tool_result_message(tool_use_id="t1", result_text="ok")])
    sent = await _sent(state, ["REM"])
    assert len(sent) == 2
    assert [b["type"] for b in sent[-1]["content"]] == ["tool_result", "text"]
    assert sent[-1]["content"][-1]["text"] == "REM"


@pytest.mark.asyncio
async def test_trailing_assistant_gets_a_new_user_message():
    sent = await _sent(LoopState(messages=[_user("hi"), _assistant("ok")]), ["REM"])
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert _texts(sent[-1]) == ["REM"]


@pytest.mark.asyncio
async def test_run_one_turn_does_not_render_todos():
    # Rendering the todo list is the engine's job (step() passes it in
    # turn_reminders); run_one_turn sends only the reminders it receives.
    state = LoopState(messages=[_user("hi")], todos=[{"content": "collect", "status": "in_progress"}])
    sent = await _sent(state)
    assert _texts(sent[-1]) == ["hi"]


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
