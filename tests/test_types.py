from friday_agent.messages.types import create_user_message, create_tool_result_message


def test_user_message_wraps_text():
    m = create_user_message("hi")
    assert m.type == "user" and m.role == "user"
    assert m.content[0].type == "text" and m.content[0].text == "hi"


def test_tool_result_message_is_user_role():
    m = create_tool_result_message(tool_use_id="t1", result_text="ok")
    assert m.role == "user"            # tool_result messages must have role "user" (alternation rule)
    block = m.content[0]
    assert block.type == "tool_result" and block.tool_use_id == "t1"
    assert block.is_error is False


def test_tool_result_error_wraps_marker():
    m = create_tool_result_message(tool_use_id="t1", result_text="boom", is_error=True)
    assert m.content[0].is_error is True
    assert "<tool_use_error>" in m.content[0].content


