"""ExampleTool — a minimal demo tool stub.

Illustrates the custom-tool pattern: subclass Tool and implement ``call``
and ``_dispatch``. The ``_dispatch`` method is a placeholder; replace it
with real external API or database calls when building a production tool.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from friday_agent.tools.base import Tool, ToolResult


class ExampleInput(BaseModel):
    payload: str = Field(description="Input data passed to the tool")
    mutating: bool = Field(False, description="Set to True if the call modifies external state (disables parallelism)")


class ExampleTool(Tool):
    """Demo tool that echoes its input. Replace the body with external API or DB calls."""

    name = "ExampleTool"

    def input_schema(self) -> type[BaseModel]:
        return ExampleInput

    def is_concurrency_safe(self, input_data: dict) -> bool:
        # Only read-only (non-mutating) calls are safe to run in parallel
        return not input_data.get("mutating", False)

    async def call(self, args: dict) -> ToolResult:
        parsed = ExampleInput(**args)
        # Replace this with an external API or DB call
        result = await self._dispatch(parsed.payload, mutating=parsed.mutating)
        return ToolResult(data=result)

    async def _dispatch(self, payload: str, *, mutating: bool) -> str:
        # Stub: return the real service response here
        return f"processed: {payload}"
