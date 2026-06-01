"""Memory system-prompt assembly: static instructions + auto-generated index.

build_memory_section() is async (it reads the store's index). The engine builds it
every turn and prepends it to the base system prompt.
"""
from __future__ import annotations

from friday_agent.memory.store import IndexEntry, MemoryStore

MEMORY_INSTRUCTIONS: str = """# Memory
You have a persistent memory that survives across sessions, accumulated over time.
Use the memory tools to save and recall typed facts.

## Types of memory
 - user: who the user is (role, preferences, expertise).
 - feedback: how the user wants you to work (corrections, confirmed approaches). Include the why.
 - project: ongoing work/goals/constraints not derivable from code or git.
 - reference: pointers to external resources (URLs, dashboards, tickets).

## When to save
Save when you learn a durable fact in one of the four types. For feedback/project,
structure the body as the rule/fact, then **Why:** and **How to apply:** lines.
Reuse an existing name to UPDATE rather than duplicate.

## What NOT to save
Do not save what is derivable (code structure, architecture, git history), one-off
debugging fixes, or ephemeral conversation/run state.

## When to access
Read a memory when it is relevant or the user asks. If a memory names a file,
function, or flag, verify it still exists before relying on it — a memory saying X
does not guarantee X exists now. If the user says to ignore memory, act as if empty.

The memory index below is rebuilt each turn from your saved memories; a memory you
save appears in your tool_result immediately and in the index from the next turn on."""


def render_index(entries: list[IndexEntry]) -> str:
    """Render the metadata index as injectable text."""
    if not entries:
        return (
            "## Current memory index\n"
            "Your memory is empty. Save memories as you learn about the user, "
            "their feedback, and the project."
        )
    lines = "\n".join(f"- [{e.type.value}] {e.name} — {e.description}" for e in entries)
    return f"## Current memory index\n{lines}"


async def build_memory_section(store: MemoryStore) -> str:
    """Assemble the always-injected memory section: instructions then live index."""
    index = await store.load_index()
    return f"{MEMORY_INSTRUCTIONS}\n\n{render_index(index)}"
