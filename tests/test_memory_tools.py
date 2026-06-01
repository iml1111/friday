"""Tests for friday_agent/memory/tool.py + MemoryStore.tools() default surface."""
import time

import pytest

from friday_agent.memory.store import InMemoryStore, MemoryEntry, MemoryType
from friday_agent.memory.tool import MemoryDelete, MemoryRead, MemorySave


def test_default_tools_are_save_read_delete():
    store = InMemoryStore()
    tools = store.tools()
    assert [t.name for t in tools] == ["memory_save", "memory_read", "memory_delete"]


def test_read_is_concurrency_safe_writes_are_not():
    store = InMemoryStore()
    assert MemoryRead(store).is_concurrency_safe({}) is True
    assert MemorySave(store).is_concurrency_safe({}) is False
    assert MemoryDelete(store).is_concurrency_safe({}) is False


@pytest.mark.asyncio
async def test_memory_save_delegates_to_store():
    store = InMemoryStore()
    result = await MemorySave(store).call(
        {"name": "u", "description": "d", "type": "user", "body": "B"}
    )
    assert result.is_error is False
    assert "u" in result.data
    assert (await store.read("u")).body == "B"


@pytest.mark.asyncio
async def test_memory_read_returns_body():
    store = InMemoryStore()
    await store.save(MemoryEntry(name="u", description="d", type=MemoryType.user, body="HELLO"))
    result = await MemoryRead(store).call({"name": "u"})
    assert "HELLO" in result.data


@pytest.mark.asyncio
async def test_memory_read_missing_is_error():
    result = await MemoryRead(InMemoryStore()).call({"name": "nope"})
    assert result.is_error is True


@pytest.mark.asyncio
async def test_memory_read_appends_staleness_caveat_when_old():
    store = InMemoryStore()
    await store.save(MemoryEntry(
        name="u", description="d", type=MemoryType.user, body="HELLO",
        updated_at=time.time() - 3 * 86400,   # 3 days ago
    ))
    result = await MemoryRead(store).call({"name": "u"})
    assert "days ago" in result.data


@pytest.mark.asyncio
async def test_memory_read_no_caveat_when_fresh():
    store = InMemoryStore()
    await store.save(MemoryEntry(
        name="u", description="d", type=MemoryType.user, body="HELLO",
        updated_at=time.time(),
    ))
    result = await MemoryRead(store).call({"name": "u"})
    assert "days ago" not in result.data


@pytest.mark.asyncio
async def test_memory_delete_delegates_to_store():
    store = InMemoryStore()
    await store.save(MemoryEntry(name="u", description="d", type=MemoryType.user, body="B"))
    result = await MemoryDelete(store).call({"name": "u"})
    assert result.is_error is False
    assert await store.read("u") is None


def test_memory_save_schema_exposes_memory_types():
    import json

    blob = json.dumps(MemorySave(InMemoryStore()).get_tool_schema()["input_schema"])
    assert "$ref" not in blob
    # The MemoryType enum is factored into $defs; it must be inlined for the model.
    assert all(t in blob for t in ("user", "feedback", "project", "reference"))
