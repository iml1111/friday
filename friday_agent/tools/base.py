"""Tool ABC and ToolResult types."""
from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Tool ABC
# ---------------------------------------------------------------------------

class Tool(ABC):
    """Core interface for agent tools.

    Subclasses must implement ``input_schema`` and ``call``.
    All other methods have safe defaults and may be overridden as needed.
    """

    name: str                          # unique tool identifier

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
        # Drop the cosmetic top-level title. $defs/$ref are already inlined away by
        # _inline_defs, so nested models/enums (e.g. TodoItem.status) reach the model.
        schema.pop("title", None)
        return {
            "name": self.name,
            "description": self.__class__.__doc__ or "",
            "input_schema": schema,
        }
