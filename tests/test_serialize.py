"""Serde round-trips for messages and state via type to_dict/from_dict methods."""
import json

from friday_agent.core.state import LoopState
from friday_agent.messages.types import Message, ContentBlock, create_user_message


def _sample_messages():
    user = create_user_message("hello")
    assistant = Message(
        type="assistant", role="assistant",
        content=[
            ContentBlock(type="thinking", text="let me think", signature="sig-xyz"),
            ContentBlock(type="text", text="calling"),
            ContentBlock(type="tool_use", id="tu1", name="ExampleTool", input={"payload": "x"}),
        ],
    )
    tool = Message(
        type="user", role="user",
        content=[ContentBlock(type="tool_result", tool_use_id="tu1", content="ok", is_error=False)],
    )
    return [user, assistant, tool]


def test_message_roundtrip_preserves_all_fields():
    for m in _sample_messages():
        d = m.to_dict()
        d2 = json.loads(json.dumps(d, ensure_ascii=False))
        back = Message.from_dict(d2)
        assert back.uuid == m.uuid
        assert back.type == m.type
        assert back.role == m.role
        assert back.is_compact_summary == m.is_compact_summary
        assert back.is_meta == m.is_meta
        assert len(back.content) == len(m.content)
        for cb_back, cb in zip(back.content, m.content):
            assert cb_back.type == cb.type
            assert cb_back.text == cb.text
            assert cb_back.id == cb.id
            assert cb_back.name == cb.name
            assert cb_back.input == cb.input
            assert cb_back.tool_use_id == cb.tool_use_id
            assert cb_back.content == cb.content
            assert cb_back.is_error == cb.is_error
            assert cb_back.signature == cb.signature


def test_loop_state_roundtrip():
    s = LoopState(messages=_sample_messages(), turn_count=3)
    d = json.loads(json.dumps(s.to_dict(), ensure_ascii=False))
    back = LoopState.from_dict(d)
    assert back.turn_count == 3
    assert len(back.messages) == len(s.messages)


def test_loop_state_roundtrip_defaults():
    s = LoopState(messages=[create_user_message("hi")])
    back = LoopState.from_dict(json.loads(json.dumps(s.to_dict())))
    assert back.turn_count == 1


def test_loop_state_roundtrip_preserves_todos():
    todos = [{"content": "A", "status": "in_progress"}, {"content": "B", "status": "pending"}]
    s = LoopState(messages=_sample_messages(), turn_count=3, todos=todos)
    back = LoopState.from_dict(json.loads(json.dumps(s.to_dict(), ensure_ascii=False)))
    assert back.todos == todos


def test_loop_state_from_dict_without_todos_defaults_empty():
    # Legacy state dict (pre-todos) must not break.
    s = LoopState.from_dict({"messages": [], "turn_count": 2})
    assert s.todos == []
