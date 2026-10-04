"""ToolResult.image — tool_result content becomes a [text, image] block array."""
import json

import pytest
from pydantic import BaseModel

from friday_agent.api.anthropic_provider import AnthropicProvider
from friday_agent.api.configs import AnthropicConfig
from friday_agent.api.openai_provider import OpenAIProvider
from friday_agent.api.provider import AssistantResponse, StopReason, TextBlock, TokenUsage, ToolUseBlock
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState
from friday_agent.messages.normalize import normalize_for_api
from friday_agent.messages.types import create_tool_result_message, create_user_message
from friday_agent.tools.base import Tool, ToolResult
from friday_agent.tools.builtin.example_tool import ExampleTool
from friday_agent.tools.orchestrator import to_tool_result_message
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider

IMAGE = {"media_type": "image/png", "data": "iVBORw0KGgo="}
MARKER = "[image omitted: not supported by the OpenAI adapter]"


class _NoInput(BaseModel):
    pass


class _Screenshot(Tool):
    """Returns a screenshot."""

    name = "screenshot"

    def input_schema(self) -> type[BaseModel]:
        return _NoInput

    async def call(self, args: dict) -> ToolResult:
        return ToolResult(data="captured", image=IMAGE)


def test_message_without_image_keeps_string_content():
    assert create_tool_result_message("t1", "ok").content[0].content == "ok"


def test_message_with_image_is_text_then_image_blocks():
    msg = create_tool_result_message("t1", "ok", image=IMAGE)
    assert msg.content[0].content == [
        {"type": "text", "text": "ok"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}},
    ]


def test_error_with_image_wraps_text_only():
    blocks = create_tool_result_message("t1", "bad", is_error=True, image=IMAGE).content[0].content
    assert blocks[0] == {"type": "text", "text": "<tool_use_error>bad</tool_use_error>"}
    assert blocks[1]["type"] == "image"


def test_to_tool_result_message_carries_data_error_and_image():
    msg = to_tool_result_message("t1", ToolResult(data=42, is_error=True, image=IMAGE))
    block = msg.content[0]
    assert block.tool_use_id == "t1" and block.is_error
    assert block.content[0]["text"] == "<tool_use_error>42</tool_use_error>"
    assert block.content[1]["source"]["data"] == IMAGE["data"]


def test_serde_round_trip_keeps_block_array():
    state = LoopState(messages=[create_tool_result_message("t1", "ok", image=IMAGE)])
    restored = LoopState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert restored.messages[0].content[0].content == state.messages[0].content[0].content


def test_anthropic_request_carries_image_block():
    api = normalize_for_api([create_tool_result_message("t1", "ok", image=IMAGE)])
    params = AnthropicProvider(api_key="k", model="m")._build_params(api, "", [], AnthropicConfig())
    block = params["messages"][-1]["content"][0]
    assert block["type"] == "tool_result"
    assert block["content"][1] == {"type": "image", "source": {"type": "base64", **IMAGE}}


def test_openai_request_flattens_image_to_marker():
    api = normalize_for_api([create_tool_result_message("t1", "ok", image=IMAGE)])
    out = OpenAIProvider._to_openai_messages(api, "")
    assert out == [{"role": "tool", "tool_call_id": "t1", "content": f"ok\n{MARKER}"}]
    assert IMAGE["data"] not in json.dumps(out)


def test_openai_plain_string_result_unchanged():
    api = normalize_for_api([create_tool_result_message("t1", "plain")])
    assert OpenAIProvider._to_openai_messages(api, "") == [{"role": "tool", "tool_call_id": "t1", "content": "plain"}]


@pytest.mark.asyncio
async def test_tool_image_reaches_next_request():
    call = AssistantResponse(
        content=[ToolUseBlock(id="t1", name="screenshot", input={})],
        stop_reason=StopReason.TOOL_USE,
        usage=TokenUsage(),
    )
    done = AssistantResponse(content=[TextBlock(text="I see it")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    fake = FakeLLMProvider(responses=[call, done])
    engine = FridayAgent(provider=fake, tools=[_Screenshot()])

    _, state = await collect_turn(engine, LoopState(messages=[create_user_message("look")]))
    await collect_turn(engine, state)

    result_block = fake.received_messages[1][-1]["content"][0]
    assert result_block["content"][1]["type"] == "image"


@pytest.mark.asyncio
async def test_result_without_image_stays_a_string_in_the_request():
    call = AssistantResponse(
        content=[ToolUseBlock(id="t1", name="ExampleTool", input={"payload": "x"})],
        stop_reason=StopReason.TOOL_USE,
        usage=TokenUsage(),
    )
    done = AssistantResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN, usage=TokenUsage())
    fake = FakeLLMProvider(responses=[call, done])
    engine = FridayAgent(provider=fake, tools=[ExampleTool()])

    _, state = await collect_turn(engine, LoopState(messages=[create_user_message("go")]))
    await collect_turn(engine, state)

    assert fake.received_messages[1][-1]["content"][0]["content"] == "processed: x"
