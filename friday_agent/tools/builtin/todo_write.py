"""TodoWrite — structured task-list tool.

Declares a `todos` state_effect; the loop (the sole state writer) applies it.
The tool is intentionally stateless: it does not touch LoopState directly, so
parallel execution can never race on shared state. Reminder rendering lives in
core/loop.py (the loop owns the todos-state concept), not here.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from friday_agent.tools.base import Tool, ToolResult


# Self-evident field descriptions (enum values, content) are not sent on the
# wire; the full-replace rule is owned solely by the tool description.
class TodoItem(BaseModel):
    content: str
    status: Literal["pending", "in_progress", "completed"]


class TodoWriteInput(BaseModel):
    todos: list[TodoItem]


class TodoWrite(Tool):
    """Model-facing text lives in `description`; this docstring is developer-only."""

    name = "TodoWrite"
    description = (
        "Manage the session task list shown to the user. Use for multi-step work "
        "(3+ steps). Send the ENTIRE list every call (full replace); keep exactly "
        "one item in_progress; mark items completed the moment they are done."
    )

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
