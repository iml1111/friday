"""scripts/verify/verify_deferred.py — its "API accepted" check, driven offline with a fake provider."""
import importlib.util
from pathlib import Path

import pytest

from friday_agent.api.provider import AssistantResponse, LLMError, StopReason, TokenUsage, ToolUseBlock
from friday_agent.core.engine import FridayAgent
from friday_agent.tools.builtin.example_tool import ExampleTool
from tests.fakes import FakeLLMProvider

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify" / "verify_deferred.py"
_spec = importlib.util.spec_from_file_location("verify_deferred", _SCRIPT)
verify_deferred = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_deferred)


def _calls(*blocks: ToolUseBlock) -> AssistantResponse:
    return AssistantResponse(content=list(blocks), stop_reason=StopReason.TOOL_USE, usage=TokenUsage())


def _accepted(checks: dict[str, bool]) -> list[bool]:
    return [ok for label, ok in checks.items() if "accepted" in label]


FIRST = _calls(
    ToolUseBlock(id="call_A", name="approval", input={"action": "publish"}),
    ToolUseBlock(id="call_E", name="ExampleTool", input={"payload": "draft"}),
)


@pytest.mark.asyncio
async def test_model_calling_approval_again_still_counts_as_accepted():
    """The resumed request went through: the model answered with a new call, not a rejection."""
    recall = _calls(ToolUseBlock(id="call_B", name="approval", input={"action": "publish"}))
    fake = FakeLLMProvider(responses=[FIRST, recall])
    engine = FridayAgent(provider=fake, tools=[verify_deferred.Approval(), ExampleTool()])

    checks = await verify_deferred._phase(engine, cancel=False)

    assert fake.call_count == 2
    assert _accepted(checks) == [True]


@pytest.mark.asyncio
async def test_rejected_resumed_request_is_not_accepted():
    fake = FakeLLMProvider(responses=[FIRST], errors={1: LLMError("400: invalid message order")})
    engine = FridayAgent(provider=fake, tools=[verify_deferred.Approval(), ExampleTool()])

    checks = await verify_deferred._phase(engine, cancel=False)

    assert _accepted(checks) == [False]
