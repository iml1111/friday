import pytest

from friday_agent.tools.builtin.example_tool import ExampleTool


@pytest.mark.asyncio
async def test_readonly_is_concurrency_safe():
    t = ExampleTool()
    assert t.is_concurrency_safe({"payload": "a", "mutating": False}) is True
    assert t.is_concurrency_safe({"payload": "a", "mutating": True}) is False


@pytest.mark.asyncio
async def test_call_returns_result():
    t = ExampleTool()
    r = await t.call({"payload": "hello", "mutating": False})
    assert "hello" in str(r.data)
