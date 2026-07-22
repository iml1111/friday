"""Breakpoint placement of _apply_cache_control: reminder-skip.

Turn-local reminders (`<system-reminder>` blocks) are non-persistent: they
vanish from their position on the next call (re-rendered onto the new last
user message). A breakpoint ON a reminder therefore creates a cache entry
that never gets a hit, and the previous turn's new tool_result is
cache-written a second time on the following call (measured in production:
~35% of session cache writes wasted). Breakpoints must land on the last
PERSISTENT block, skipping trailing reminders.
"""
from friday_agent.api.anthropic_provider import AnthropicProvider

_EPHEMERAL = {"type": "ephemeral"}


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _tool_result(content: str) -> dict:
    return {"type": "tool_result", "tool_use_id": "tu_1", "content": content}


def _reminder(t: str = "todos") -> dict:
    return _text(f"<system-reminder>\n{t}\n</system-reminder>")


def _marked(block: dict) -> bool:
    return block.get("cache_control") == _EPHEMERAL


def _apply(messages: list, system: list | None = None) -> dict:
    params = {"system": system if system is not None else [_text("SYS")], "messages": messages}
    AnthropicProvider._apply_cache_control(params)
    return params


def test_trailing_reminder_skipped_breakpoint_on_persistent_block():
    msgs = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "tu_1", "name": "Read", "input": {}}]},
        {"role": "user", "content": [_tool_result("file contents"), _reminder()]},
    ]
    p = _apply(msgs)
    last = p["messages"][-1]["content"]
    assert _marked(last[0])  # persistent block (tool_result) carries the mark
    assert not _marked(last[1])  # reminder stays outside the cache


def test_multiple_trailing_reminders_all_skipped():
    msgs = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "tu_1", "name": "Read", "input": {}}]},
        {"role": "user", "content": [
            _tool_result("ok"), _reminder("todo"), _reminder("memory index"),
        ]},
    ]
    p = _apply(msgs)
    last = p["messages"][-1]["content"]
    assert _marked(last[0])
    assert not any(_marked(b) for b in last[1:])


def test_all_reminder_message_gets_no_mark_anchor_covers():
    msgs = [
        {"role": "user", "content": [_text("user text")]},
        {"role": "user", "content": [_reminder()]},  # defensive branch: reminders only
    ]
    p = _apply(msgs)
    assert not any(_marked(b) for b in p["messages"][-1]["content"])
    assert _marked(p["messages"][-2]["content"][-1])  # -2 anchor untouched


def test_no_reminder_behaves_as_before():
    msgs = [
        {"role": "assistant", "content": [_text("a"), {"type": "tool_use", "id": "t", "name": "Read", "input": {}}]},
        {"role": "user", "content": [_tool_result("ok")]},
    ]
    p = _apply(msgs)
    assert _marked(p["messages"][-1]["content"][-1])  # last block, as before
    assert _marked(p["messages"][-2]["content"][-1])


def test_user_text_with_trailing_reminder_marks_the_text():
    # First turn of a session: [user text, reminder] -> mark the user text.
    msgs = [{"role": "user", "content": [_text("find the backend"), _reminder()]}]
    p = _apply(msgs)
    blocks = p["messages"][-1]["content"]
    assert _marked(blocks[0]) and not _marked(blocks[1])


def test_system_breakpoint_unchanged():
    p = _apply([{"role": "user", "content": [_text("hi")]}], system=[_text("A"), _text("B")])
    assert _marked(p["system"][-1])
    assert not _marked(p["system"][0])
