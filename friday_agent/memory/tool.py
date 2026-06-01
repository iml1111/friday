"""Memory tools — thin agent surface wrapping a MemoryStore.

Each tool returns a plain ToolResult (NO state_effect): memory lives in the Store,
not in LoopState. Reads are concurrency-safe; writes/deletes are not.
"""
from __future__ import annotations

import time

from pydantic import BaseModel, Field

from friday_agent.memory.store import MemoryEntry, MemoryStore, MemoryType
from friday_agent.tools.base import Tool, ToolResult

_STALENESS_SECONDS = 86400  # 1 day


class MemorySaveInput(BaseModel):
    name: str = Field(description="Stable kebab-case id; reuse the same name to UPDATE an existing memory")
    description: str = Field(description="One-line summary shown in the memory index; used to judge relevance later — be specific")
    type: MemoryType = Field(description="user | feedback | project | reference")
    body: str = Field(description="Memory content. For feedback/project, structure as the rule/fact, then **Why:** and **How to apply:** lines")


class MemoryReadInput(BaseModel):
    name: str = Field(description="The memory's stable name (as shown in the index)")


class MemoryDeleteInput(BaseModel):
    name: str = Field(description="The memory's stable name to delete")


class MemorySave(Tool):
    """Save or update a long-term memory that persists across sessions. Use when you
    learn something about the user, their feedback on how to work, project context not
    derivable from code, or a pointer to an external system. Reuse an existing name to
    update rather than duplicate. Do NOT save code/architecture/git facts or ephemeral
    conversation state."""

    name = "memory_save"

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def input_schema(self) -> type[BaseModel]:
        return MemorySaveInput

    def is_concurrency_safe(self, input_data: dict) -> bool:
        return False

    async def call(self, args: dict) -> ToolResult:
        inp = MemorySaveInput(**args)
        await self._store.save(
            MemoryEntry(name=inp.name, description=inp.description, type=inp.type, body=inp.body)
        )
        return ToolResult(data=f"Saved {inp.type.value} memory '{inp.name}'.")


class MemoryRead(Tool):
    """Read the full body of a saved memory by name. Use when a memory in the index
    looks relevant to the current task. If the memory names a file, function, or flag,
    verify it still exists before relying on it."""

    name = "memory_read"

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def input_schema(self) -> type[BaseModel]:
        return MemoryReadInput

    def is_concurrency_safe(self, input_data: dict) -> bool:
        return True

    async def call(self, args: dict) -> ToolResult:
        inp = MemoryReadInput(**args)
        entry = await self._store.read(inp.name)
        if entry is None:
            return ToolResult(data=f"No memory named '{inp.name}'.", is_error=True)
        body = entry.body
        if entry.updated_at is not None:
            age_days = int((time.time() - entry.updated_at) / 86400)
            if (time.time() - entry.updated_at) > _STALENESS_SECONDS:
                body += (
                    f"\n\n(This memory was written {age_days} days ago; verify any named "
                    f"file/function/flag still exists before relying on it.)"
                )
        return ToolResult(data=body)


class MemoryDelete(Tool):
    """Delete a memory by name. Use to remove a memory that is wrong or no longer true."""

    name = "memory_delete"

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def input_schema(self) -> type[BaseModel]:
        return MemoryDeleteInput

    def is_concurrency_safe(self, input_data: dict) -> bool:
        return False

    async def call(self, args: dict) -> ToolResult:
        inp = MemoryDeleteInput(**args)
        await self._store.delete(inp.name)
        return ToolResult(data=f"Deleted memory '{inp.name}'.")
