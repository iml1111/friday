from typing import Literal

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


def test_tool_result_state_effect_default_and_set():
    assert ToolResult(data="ok").state_effect is None
    r = ToolResult(data="ok", state_effect={"todos": [{"content": "A", "status": "pending"}]})
    assert r.state_effect == {"todos": [{"content": "A", "status": "pending"}]}


class _Item(BaseModel):
    kind: Literal["a", "b", "c"]


class _NestedIn(BaseModel):
    items: list[_Item]


class _NestedTool(Tool):
    name = "Nested"

    def input_schema(self):
        return _NestedIn

    async def call(self, args):
        return ToolResult(data="ok")


def test_get_tool_schema_inlines_defs_and_exposes_enum():
    import json

    sent = _NestedTool().get_tool_schema()["input_schema"]
    blob = json.dumps(sent)
    assert "$ref" not in blob and "$defs" not in sent      # fully flat (B안)
    assert all(v in blob for v in ("a", "b", "c"))          # enum inlined → visible


def test_todowrite_schema_exposes_status_enum():
    import json

    from friday_agent.tools.builtin.todo_write import TodoWrite

    blob = json.dumps(TodoWrite().get_tool_schema()["input_schema"])
    assert "$ref" not in blob
    # Regression: the dangling-$ref bug dropped these, so the model sent status='todo'.
    assert all(s in blob for s in ("pending", "in_progress", "completed"))
