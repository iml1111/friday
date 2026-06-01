"""TodoWrite — structured task-list tool.

Declares a `todos` state_effect; the loop (the sole state writer) applies it.
The tool is intentionally stateless: it does not touch LoopState directly, so
parallel execution can never race on shared state. Reminder rendering lives in
core/loop.py (the loop owns the todos-state concept), not here.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from friday_agent.tools.base import Tool, ToolResult


class TodoItem(BaseModel):
    content: str = Field(description="Imperative task description, e.g. 'Add the login endpoint'")
    status: Literal["pending", "in_progress", "completed"] = Field(
        description="pending = not started, in_progress = active now, completed = done"
    )


class TodoWriteInput(BaseModel):
    todos: list[TodoItem] = Field(
        description="The COMPLETE updated list; it replaces the previous list entirely"
    )


class TodoWrite(Tool):
    """Create and manage a structured task list for the current session. Use for
    multi-step work (3+ steps) to track progress and show the user a plan. Send the
    ENTIRE updated list every call — it replaces the previous one. Keep exactly one
    item in_progress at a time; mark items completed the moment they are done."""

    name = "TodoWrite"

    def input_schema(self) -> type[BaseModel]:
        return TodoWriteInput

    def is_concurrency_safe(self, input_data: dict) -> bool:
        return False  # mutates tracked state -> never batched in parallel

    async def call(self, args: dict) -> ToolResult:
        todos = [t.model_dump() for t in TodoWriteInput(**args).todos]
        return ToolResult(
            data=f"Updated todo list ({len(todos)} items).",
            state_effect={"todos": todos},
        )
