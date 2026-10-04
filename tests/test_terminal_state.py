"""Terminal.state — every Terminal the loop emits carries the state to persist."""
import pytest

from friday_agent.api.provider import AssistantResponse, LLMError, StopReason, TextBlock, TokenUsage
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import create_user_message
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider


def _text(text: str) -> AssistantResponse:
    return AssistantResponse(content=[TextBlock(text=text)], stop_reason=StopReason.END_TURN, usage=TokenUsage())


@pytest.mark.asyncio
async def test_completed_terminal_carries_input_plus_assistant():
    fake = FakeLLMProvider(responses=[_text("hello")])
    engine = FridayAgent(provider=fake)
    todos = [{"content": "a", "status": "in_progress"}]
    state = LoopState(messages=[create_user_message("hi")], turn_count=3, todos=todos)

    messages, outcome = await collect_turn(engine, state)

    assert isinstance(outcome, Terminal) and outcome.reason == "completed"
    assert len(messages) == 1 and messages[0].type == "assistant"
    assert outcome.state.messages == [*state.messages, messages[0]]
    assert outcome.state.todos == todos
    assert outcome.state.turn_count == 4


@pytest.mark.asyncio
async def test_model_error_terminal_carries_input_state_for_retry():
    fake = FakeLLMProvider(responses=[_text("recovered")], errors={0: LLMError("boom")})
    engine = FridayAgent(provider=fake)
    state = LoopState(messages=[create_user_message("hi")], turn_count=2)

    _, outcome = await collect_turn(engine, state)
    assert outcome.reason == "model_error"
    assert outcome.state is state

    _, retried = await collect_turn(engine, outcome.state)
    assert retried.reason == "completed"
    assert fake.received_messages[1] == fake.received_messages[0]


@pytest.mark.asyncio
async def test_completed_state_survives_serde_and_continues():
    fake = FakeLLMProvider(responses=[_text("first"), _text("second")])
    engine = FridayAgent(provider=fake)
    _, first = await collect_turn(engine, LoopState(messages=[create_user_message("q1")]))

    restored = LoopState.from_dict(first.state.to_dict())
    restored.messages.append(create_user_message("q2"))
    _, second = await collect_turn(engine, restored)

    assert second.reason == "completed"
    assert [m["role"] for m in fake.received_messages[1]] == ["user", "assistant", "user"]
