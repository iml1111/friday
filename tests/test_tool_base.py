from pydantic import BaseModel

from friday_agent.tools.base import Tool, ToolResult


class _In(BaseModel):
    x: int = 0


class _T(Tool):
    name = "T"

    def input_schema(self):
        return _In

    async def call(self, args):
        return ToolResult(data="ok")


def test_conservative_defaults():
    t = _T()
    assert t.is_concurrency_safe({}) is False   # conservative default: not concurrency-safe


def test_tool_schema_shape():
    schema = _T().get_tool_schema()
    assert set(schema) >= {"name", "description", "input_schema"}
    assert schema["name"] == "T"
