"""Deferred tools — Tool.is_deferred, Suspended, resume()."""
import json

import pytest
from pydantic import BaseModel, Field

from friday_agent.api.provider import AssistantResponse, StopReason, TextBlock, TokenUsage, ToolUseBlock
from friday_agent.core.engine import FridayAgent
from friday_agent.core.loop import pending_tool_uses, resume
from friday_agent.core.state import LoopState, PendingToolUseError, Suspended, Terminal
from friday_agent.messages.types import create_user_message
from friday_agent.tools.base import Tool, ToolResult
from friday_agent.tools.builtin.example_tool import ExampleTool
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider


class ApprovalInput(BaseModel):
    action: str = Field(description="What needs approval")


class Approval(Tool):
    """Asks a human to approve an action; the answer arrives later."""

    name = "approval"

    def __init__(self) -> None:
        self.calls = 0

    def input_schema(self) -> type[BaseModel]:
        return ApprovalInput

    def is_deferred(self, input: dict) -> bool:
        return True

    async def call(self, args: dict) -> ToolResult:
        self.calls += 1
        return ToolResult(data="approval runs outside step()", is_error=True)


def _calls(*blocks: ToolUseBlock) -> AssistantResponse:
    return AssistantResponse(content=list(blocks), stop_reason=StopReason.TOOL_USE, usage=TokenUsage())


def _approval(tool_id: str) -> ToolUseBlock:
    return ToolUseBlock(id=tool_id, name="approval", input={"action": "send"})


def _example(tool_id: str) -> ToolUseBlock:
    return ToolUseBlock(id=tool_id, name="ExampleTool", input={"payload": "x"})


def _text(text: str = "done") -> AssistantResponse:
    return AssistantResponse(content=[TextBlock(text=text)], stop_reason=StopReason.END_TURN, usage=TokenUsage())


def _result_ids(state: LoopState) -> list[str]:
    return [b.tool_use_id for m in state.messages for b in m.content if b.type == "tool_result"]


def _start() -> LoopState:
    return LoopState(messages=[create_user_message("go")])


@pytest.mark.asyncio
async def test_deferred_only_call_suspends_without_running():
    tool = Approval()
    fake = FakeLLMProvider(responses=[_calls(_approval("a1"))])

    messages, outcome = await collect_turn(FridayAgent(provider=fake, tools=[tool]), _start())

    assert isinstance(outcome, Suspended)
    assert tool.calls == 0
    assert [b.id for b in outcome.pending] == ["a1"]
    assert [m.type for m in messages] == ["assistant"]  # no tool_result yielded
    assert outcome.state.turn_count == 2
    restored = LoopState.from_dict(json.loads(json.dumps(outcome.state.to_dict())))
    assert restored.to_dict() == outcome.state.to_dict()
    assert [b.id for b in pending_tool_uses(restored)] == ["a1"]


@pytest.mark.asyncio
async def test_mixed_response_runs_normal_tool_and_holds_deferred():
    fake = FakeLLMProvider(responses=[_calls(_approval("a1"), _example("e1"))])

    _, outcome = await collect_turn(FridayAgent(provider=fake, tools=[Approval(), ExampleTool()]), _start())

    assert isinstance(outcome, Suspended)
    assert _result_ids(outcome.state) == ["e1"]
    assert [b.id for b in outcome.pending] == ["a1"]


@pytest.mark.asyncio
async def test_full_resume_orders_results_and_step_continues():
    fake = FakeLLMProvider(responses=[_calls(_approval("a1"), _example("e1")), _text("approved and done")])
    engine = FridayAgent(provider=fake, tools=[Approval(), ExampleTool()])
    _, suspended = await collect_turn(engine, _start())

    state = resume(suspended.state, {"a1": ToolResult(data="approved")})

    assert _result_ids(state) == ["a1", "e1"]  # tool_use order, not arrival order
    assert pending_tool_uses(state) == []
    assert state.turn_count == suspended.state.turn_count
    _, outcome = await collect_turn(engine, state)
    assert isinstance(outcome, Terminal) and outcome.reason == "completed"
    sent = fake.received_messages[1]
    assert [b["tool_use_id"] for m in sent for b in m["content"] if b["type"] == "tool_result"] == ["a1", "e1"]


@pytest.mark.asyncio
async def test_partial_resume_keeps_rest_pending():
    fake = FakeLLMProvider(responses=[_calls(_approval("a1"), _approval("a2"))])
    engine = FridayAgent(provider=fake, tools=[Approval()])
    _, suspended = await collect_turn(engine, _start())

    state = resume(suspended.state, {"a2": ToolResult(data="ok")})

    assert [b.id for b in pending_tool_uses(state)] == ["a1"]
    with pytest.raises(PendingToolUseError) as exc:
        await collect_turn(engine, state)
    assert exc.value.tool_use_ids == ["a1"]
    assert fake.call_count == 1


@pytest.mark.asyncio
async def test_cancel_flow_takes_one_model_call():
    fake = FakeLLMProvider(responses=[_calls(_approval("a1")), _text("ok, cancelled")])
    engine = FridayAgent(provider=fake, tools=[Approval()])
    _, suspended = await collect_turn(engine, _start())

    state = resume(suspended.state, {"a1": ToolResult(data="cancelled by the user", is_error=True)})
    state.messages.append(create_user_message("never mind, stop"))
    _, outcome = await collect_turn(engine, state)

    assert outcome.reason == "completed"
    assert fake.call_count == 2
    assert [m["role"] for m in fake.received_messages[1]] == ["user", "assistant", "user", "user"]


@pytest.mark.asyncio
async def test_resume_rejects_unknown_and_already_answered_ids():
    fake = FakeLLMProvider(responses=[_calls(_approval("a1"))])
    _, suspended = await collect_turn(FridayAgent(provider=fake, tools=[Approval()]), _start())

    with pytest.raises(ValueError, match="nope"):
        resume(suspended.state, {"nope": ToolResult(data="x")})
    answered = resume(suspended.state, {"a1": ToolResult(data="x")})
    with pytest.raises(ValueError, match="a1"):
        resume(answered, {"a1": ToolResult(data="again")})


@pytest.mark.asyncio
async def test_resume_inserts_results_ahead_of_appended_user_message():
    fake = FakeLLMProvider(responses=[_calls(_approval("a1")), _text()])
    engine = FridayAgent(provider=fake, tools=[Approval()])
    _, suspended = await collect_turn(engine, _start())
    early = suspended.state
    early.messages.append(create_user_message("are you there?"))

    with pytest.raises(PendingToolUseError):
        await collect_turn(engine, early)
    state = resume(early, {"a1": ToolResult(data="approved")})

    assert [m.content[0].type for m in state.messages] == ["text", "tool_use", "tool_result", "text"]
    _, outcome = await collect_turn(engine, state)
    assert outcome.reason == "completed"


@pytest.mark.asyncio
async def test_resume_applies_state_effect():
    fake = FakeLLMProvider(responses=[_calls(_approval("a1"))])
    _, suspended = await collect_turn(FridayAgent(provider=fake, tools=[Approval()]), _start())
    todos = [{"content": "ship", "status": "completed"}]

    state = resume(suspended.state, {"a1": ToolResult(data="ok", state_effect={"todos": todos})})

    assert state.todos == todos


@pytest.mark.asyncio
async def test_resume_does_not_mutate_input_and_empty_results_is_noop():
    fake = FakeLLMProvider(responses=[_calls(_approval("a1"))])
    _, suspended = await collect_turn(FridayAgent(provider=fake, tools=[Approval()]), _start())
    before = suspended.state.to_dict()

    resume(suspended.state, {"a1": ToolResult(data="ok")})

    assert suspended.state.to_dict() == before
    assert resume(suspended.state, {}).to_dict() == before


@pytest.mark.asyncio
async def test_invalid_input_to_deferred_tool_runs_inline_with_error():
    tool = Approval()
    bad = ToolUseBlock(id="a1", name="approval", input={"wrong": 1})
    fake = FakeLLMProvider(responses=[_calls(bad)])

    _, outcome = await collect_turn(FridayAgent(provider=fake, tools=[tool]), _start())

    assert isinstance(outcome, LoopState)  # not deferred: no Suspended
    assert tool.calls == 1
    assert outcome.messages[-1].content[0].is_error


@pytest.mark.asyncio
async def test_raising_predicate_on_valid_input_fails_closed():
    """A gate that crashes on a well-formed call holds it instead of running it unchecked."""

    class BrokenGate(Approval):
        def is_deferred(self, input: dict) -> bool:
            raise RuntimeError("threshold lookup failed")

    tool = BrokenGate()
    fake = FakeLLMProvider(responses=[_calls(_approval("a1"))])

    _, outcome = await collect_turn(FridayAgent(provider=fake, tools=[tool]), _start())

    assert isinstance(outcome, Suspended)
    assert tool.calls == 0
    assert [b.id for b in outcome.pending] == ["a1"]


@pytest.mark.asyncio
async def test_unknown_tool_beside_deferred_errors_immediately():
    unknown = ToolUseBlock(id="u1", name="nope", input={})
    fake = FakeLLMProvider(responses=[_calls(_approval("a1"), unknown)])

    _, outcome = await collect_turn(FridayAgent(provider=fake, tools=[Approval()]), _start())

    assert isinstance(outcome, Suspended)
    assert _result_ids(outcome.state) == ["u1"]
    assert [b.id for b in outcome.pending] == ["a1"]


@pytest.mark.asyncio
async def test_callers_without_deferred_tools_never_see_suspended():
    fake = FakeLLMProvider(responses=[_calls(_example("e1")), _text()])
    engine = FridayAgent(provider=fake, tools=[ExampleTool()])

    _, first = await collect_turn(engine, _start())
    _, second = await collect_turn(engine, first)

    assert type(first) is LoopState and isinstance(second, Terminal)
