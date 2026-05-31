"""Unit tests for normalize_for_api — internal Message list → API wire format.

normalize_for_api excludes is_meta messages, messages with no role, and messages
with empty content; it converts text / tool_use / tool_result / thinking blocks.
(Boundary-slice tests were removed with the compact-boundary model under
caller-owned compaction.)
"""
from friday_agent.messages.normalize import normalize_for_api
from friday_agent.messages.types import ContentBlock, Message, create_user_message


def test_user_text_message_converts():
    out = normalize_for_api([create_user_message("hello")])
    assert out == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]


def test_is_meta_message_excluded():
    meta = create_user_message("synthetic", is_meta=True)
    assert normalize_for_api([meta]) == []


def test_message_without_role_excluded():
    # A system-type message with empty role is internal-only and not sent to the API.
    sys_msg = Message(type="system", role="", content=[ContentBlock(type="text", text="x")])
    assert normalize_for_api([sys_msg]) == []


def test_empty_content_message_excluded():
    empty = Message(type="user", role="user", content=[])
    assert normalize_for_api([empty]) == []


def test_tool_use_block_converts():
    msg = Message(
        type="assistant", role="assistant",
        content=[ContentBlock(type="tool_use", id="t1", name="Example", input={"k": "v"})],
    )
    out = normalize_for_api([msg])
    assert out == [{
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "t1", "name": "Example", "input": {"k": "v"}}],
    }]


def test_tool_result_error_sets_is_error():
    msg = Message(
        type="user", role="user",
        content=[ContentBlock(type="tool_result", tool_use_id="t1", content="boom", is_error=True)],
    )
    out = normalize_for_api([msg])
    assert out == [{
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "boom", "is_error": True}],
    }]


def test_thinking_block_echoed():
    msg = Message(
        type="assistant", role="assistant",
        content=[ContentBlock(type="thinking", text="reasoning")],
    )
    out = normalize_for_api([msg])
    assert out == [{"role": "assistant", "content": [{"type": "thinking", "thinking": "reasoning"}]}]
