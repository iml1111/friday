"""get_tool_schema wire diet: description precedence, title stripping, slimming.

- The `description` class attribute is sent when set, with docstring fallback —
  keeps developer implementation notes (docstrings) from leaking into the
  per-request prompt prefix.
- Pydantic's auto-generated cosmetic titles are stripped recursively (pure
  token overhead); a real *field* named "title" (dict value) must survive.
- Optional ceremony (anyOf [X, null] + default: null) collapses to the plain
  type; "not required" is already carried by the required array. Validation is
  unaffected — tool calls are checked against the Pydantic model, never this
  wire schema.
"""
from typing import Any, Optional, Union

from pydantic import BaseModel, Field

from friday_agent.tools.base import Tool, ToolResult


class _Input(BaseModel):
    ref: str = Field(description="target ref")
    count: Optional[int] = None


class _DocOnlyTool(Tool):
    """Dev docstring — used as fallback description."""

    name = "doc_only"

    def input_schema(self):
        return _Input

    async def call(self, args):  # pragma: no cover — never invoked in schema tests
        return ToolResult(data="ok")


class _DescribedTool(_DocOnlyTool):
    """Dev docstring — must NOT be sent when description is set."""

    name = "described"
    description = "Model-facing description."


def _has_string_title(node) -> bool:
    if isinstance(node, dict):
        if isinstance(node.get("title"), str):
            return True
        return any(_has_string_title(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_string_title(v) for v in node)
    return False


def test_description_attr_takes_precedence_over_docstring():
    assert _DescribedTool().get_tool_schema()["description"] == "Model-facing description."


def test_docstring_fallback_when_description_unset():
    assert "fallback" in _DocOnlyTool().get_tool_schema()["description"]


def test_titles_stripped_recursively_and_fields_preserved():
    schema = _DocOnlyTool().get_tool_schema()["input_schema"]
    assert not _has_string_title(schema)
    assert set(schema["properties"]) == {"ref", "count"}
    assert schema["properties"]["ref"]["description"] == "target ref"


def test_field_literally_named_title_survives_stripping():
    class _TitleFieldInput(BaseModel):
        title: str = Field(description="a real field named title")

    class _TitleTool(_DocOnlyTool):
        name = "title_field"

        def input_schema(self):
            return _TitleFieldInput

    schema = _TitleTool().get_tool_schema()["input_schema"]
    assert "title" in schema["properties"]          # the field itself survives
    assert not _has_string_title(schema["properties"]["title"])  # its cosmetic title goes


# --- Optional expansion / null-default slimming ------------------------------

def test_optional_collapses_to_plain_type_without_null_union():
    schema = _DocOnlyTool().get_tool_schema()["input_schema"]
    count = schema["properties"]["count"]
    assert count == {"type": "integer"}             # anyOf and default:null both gone
    assert "count" not in schema.get("required", [])  # optionality owned by required


def test_optional_with_description_keeps_description():
    class _In(BaseModel):
        ref: Optional[str] = Field(None, description="target ref")

    class _T(_DocOnlyTool):
        name = "opt_desc"

        def input_schema(self):
            return _In

    prop = _T().get_tool_schema()["input_schema"]["properties"]["ref"]
    assert prop == {"type": "string", "description": "target ref"}


def test_optional_any_becomes_untyped_property():
    class _In(BaseModel):
        group: Optional[Any] = Field(None, description="reserved")

    class _T(_DocOnlyTool):
        name = "opt_any"

        def input_schema(self):
            return _In

    prop = _T().get_tool_schema()["input_schema"]["properties"]["group"]
    assert prop == {"description": "reserved"}      # empty schema = any — valid JSON Schema


def test_non_null_union_and_meaningful_default_preserved():
    class _In(BaseModel):
        mixed: Union[str, int]
        trusted: bool = Field(True, description="keep default true")

    class _T(_DocOnlyTool):
        name = "union_default"

        def input_schema(self):
            return _In

    props = _T().get_tool_schema()["input_schema"]["properties"]
    assert "anyOf" in props["mixed"]                # non-null unions untouched
    assert props["trusted"]["default"] is True      # meaningful defaults preserved


def test_description_indentation_dedented():
    class _T(_DocOnlyTool):
        name = "indented"
        description = (
            "First line rule.\n"
            "    indented continuation from a triple-quoted literal.\n"
            "  another one.  "
        )

    desc = _T().get_tool_schema()["description"]
    assert desc == (
        "First line rule.\n"
        "indented continuation from a triple-quoted literal.\n"
        "another one."
    )


def test_nested_model_docstring_description_dedented():
    class _Item(BaseModel):
        """Docstring whose newline
        leaves source indentation behind."""
        v: str

    class _In(BaseModel):
        items: list[_Item]

    class _T(_DocOnlyTool):
        name = "nested_desc"

        def input_schema(self):
            return _In

    items = _T().get_tool_schema()["input_schema"]["properties"]["items"]["items"]
    assert items["description"] == "Docstring whose newline\nleaves source indentation behind."
