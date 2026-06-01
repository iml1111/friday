"""Tests for friday_agent/memory/prompt.py — instructions + auto index assembly."""
import pytest

from friday_agent.memory.prompt import (
    MEMORY_INSTRUCTIONS,
    build_memory_section,
    render_index,
)
from friday_agent.memory.store import IndexEntry, InMemoryStore, MemoryEntry, MemoryType


def test_render_index_empty_says_empty():
    out = render_index([])
    assert "empty" in out.lower()


def test_render_index_lists_type_name_description():
    entries = [
        IndexEntry(name="user-role", description="backend eng", type=MemoryType.user),
        IndexEntry(name="testing-policy", description="real DB", type=MemoryType.feedback),
    ]
    out = render_index(entries)
    assert "[user] user-role — backend eng" in out
    assert "[feedback] testing-policy — real DB" in out


@pytest.mark.asyncio
async def test_build_section_empty_store_contains_instructions_then_empty_index():
    out = await build_memory_section(InMemoryStore())
    assert out.startswith(MEMORY_INSTRUCTIONS)
    assert "empty" in out.lower()
    # Static instructions precede the dynamic index.
    assert out.index(MEMORY_INSTRUCTIONS) < out.lower().index("empty")


@pytest.mark.asyncio
async def test_build_section_reflects_saved_entries():
    store = InMemoryStore()
    await store.save(MemoryEntry(name="user-role", description="backend eng", type=MemoryType.user, body="B"))
    out = await build_memory_section(store)
    assert "[user] user-role — backend eng" in out
    assert MEMORY_INSTRUCTIONS in out
