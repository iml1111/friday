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
        schema = self.input_schema().model_json_schema()
        # Remove Pydantic v2 metadata keys not expected by the API
        schema.pop("title", None)
        schema.pop("$defs", None)
        return {
            "name": self.name,
            "description": self.__class__.__doc__ or "",
            "input_schema": schema,
        }
