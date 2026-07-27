"""Tool ABC and ToolResult types."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """Result returned by a tool execution."""
    data: Any                          # execution result (string or structured data)
    is_error: bool = False
    state_effect: dict | None = None   # declarative loop-state mutation, e.g. {"todos": [...]}


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def _inline_defs(schema: dict) -> dict:
    """Return a self-contained schema: every $ref to #/$defs/* replaced by the
    referenced definition inlined, then $defs removed. Pydantic factors nested
    models and enums into $defs; the model never sees them unless inlined. Sibling
    keys beside a $ref (e.g. a field-level description) are merged over the resolved
    definition. Produces a flat schema with no $ref/$defs — accepted by every
    provider including OpenAI strict mode. (Assumes non-recursive models, which the
    Pydantic-defined tool inputs are.)"""
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                target = resolve(defs.get(node["$ref"].rsplit("/", 1)[-1], {}))
                siblings = {k: resolve(v) for k, v in node.items() if k != "$ref"}
                return {**target, **siblings}
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(schema)


def _strip_titles(node) -> None:
    """Remove auto-generated ``title`` annotations in place, recursively.

    Pydantic stamps every model/field with a cosmetic title derived from its
    name; the model already sees the property names, so titles are pure token
    overhead in the prompt prefix. Only string-valued ``title`` keys are
    removed — a *property* named ``title`` maps to a dict schema and survives.
    """
    if isinstance(node, dict):
        if isinstance(node.get("title"), str):
            node.pop("title")
        for value in node.values():
            _strip_titles(value)
    elif isinstance(node, list):
        for value in node:
            _strip_titles(value)


def _dedent_text(text: str) -> str:
    """Strip indentation after newlines and surrounding whitespace.

    Triple-quoted description literals leak their source indentation into the
    wire payload ("...\\n    continued"); the model gains nothing from it.
    Newlines themselves are kept — deliberate line structure survives.
    """
    return _INDENT_RE.sub("\n", text).strip()


_INDENT_RE = re.compile(r"\n[ \t]+")


def _slim_wire_schema(node) -> None:
    """Shrink Pydantic's verbose Optional encoding in place, recursively.

    ``Optional[X] = None`` serializes as ``anyOf: [X, {type: null}]`` plus
    ``default: null`` — three tokens of ceremony per field for information the
    ``required`` array already carries. Collapse the anyOf to the non-null
    member (sibling keys like description win on collision) and drop null
    defaults. Non-null unions and meaningful defaults are left untouched.
    Validation is unaffected: tool calls are checked against the Pydantic
    model, never this wire schema.
    """
    if isinstance(node, dict):
        any_of = node.get("anyOf")
        if isinstance(any_of, list) and len(any_of) == 2:
            non_null = [m for m in any_of if m != {"type": "null"}]
            if len(non_null) == 1 and isinstance(non_null[0], dict):
                member = non_null[0]
                node.pop("anyOf")
                for key, value in member.items():
                    node.setdefault(key, value)
        if "default" in node and node["default"] is None:
            node.pop("default")
        if isinstance(node.get("description"), str):
            node["description"] = _dedent_text(node["description"])
        for value in node.values():
            _slim_wire_schema(value)
    elif isinstance(node, list):
        for value in node:
            _slim_wire_schema(value)


# ---------------------------------------------------------------------------
# Tool ABC
# ---------------------------------------------------------------------------

class Tool(ABC):
    """Core interface for agent tools.

    Subclasses must implement ``input_schema`` and ``call``.
    All other methods have safe defaults and may be overridden as needed.
    """

    name: str                          # unique tool identifier

    # Model-facing description sent to the API. When empty, falls back to the
    # class docstring. Set this on tools whose docstring carries implementation
    # notes the model should not pay tokens for.
    description: str = ""

    @abstractmethod
    def input_schema(self) -> type[BaseModel]:
        """Return the Pydantic model class used to validate and document inputs."""
        ...

    # --- Conservative-default methods ---

    def is_concurrency_safe(self, input: dict) -> bool:
        """Return whether this call can run in parallel with other tools. Defaults to False (conservative)."""
        return False

    @abstractmethod
    async def call(self, args: dict) -> ToolResult:
        """Execute the tool and return a ToolResult."""
        ...

    def get_tool_schema(self) -> dict:
        """Build the tool schema dict to send to the API."""
        schema = _inline_defs(self.input_schema().model_json_schema())
        # Strip cosmetic titles everywhere (top-level and nested). $defs/$ref are
        # already inlined by _inline_defs, so nested models/enums (e.g.
        # TodoItem.status) reach the model without their auto-generated titles.
        _strip_titles(schema)
        _slim_wire_schema(schema)
        return {
            "name": self.name,
            "description": _dedent_text(self.description or (self.__class__.__doc__ or "")),
            "input_schema": schema,
        }
