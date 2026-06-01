"""Tests for friday_agent/memory/store.py — data model + prompt-section assembly."""
import pytest

from friday_agent.memory.store import (
    MEMORY_INSTRUCTIONS,
    IndexEntry,
    MemoryEntry,
    MemoryType,
    build_memory_section,
)
from tests.fakes import InMemoryStore


def test_memory_type_values():
    assert {t.value for t in MemoryType} == {"user", "feedback", "project", "reference"}


@pytest.mark.asyncio
async def test_inmemory_save_read_roundtrip():
    store = InMemoryStore()
    await store.save(MemoryEntry(name="a", description="d", type=MemoryType.user, body="B"))
    got = await store.read("a")
    assert got is not None
    assert got.name == "a" and got.body == "B" and got.type == MemoryType.user


@pytest.mark.asyncio
async def test_inmemory_read_missing_returns_none():
    assert await InMemoryStore().read("nope") is None


@pytest.mark.asyncio
async def test_inmemory_upsert_by_name_updates_not_duplicates():
    store = InMemoryStore()
    await store.save(MemoryEntry(name="a", description="d1", type=MemoryType.user, body="B1"))
    await store.save(MemoryEntry(name="a", description="d2", type=MemoryType.user, body="B2"))
    index = await store.load_index()
    assert len(index) == 1
    assert (await store.read("a")).body == "B2"


@pytest.mark.asyncio
async def test_inmemory_save_stamps_updated_at_when_absent():
    store = InMemoryStore()
    await store.save(MemoryEntry(name="a", description="d", type=MemoryType.user, body="B"))
    assert (await store.read("a")).updated_at is not None


@pytest.mark.asyncio
async def test_inmemory_load_index_is_metadata_only():
    store = InMemoryStore()
    await store.save(MemoryEntry(name="a", description="desc-a", type=MemoryType.feedback, body="BODY"))
    index = await store.load_index()
    assert len(index) == 1
    e = index[0]
    assert isinstance(e, IndexEntry)
    assert e.name == "a" and e.description == "desc-a" and e.type == MemoryType.feedback
    assert not hasattr(e, "body")


@pytest.mark.asyncio
async def test_inmemory_delete_removes_entry():
    store = InMemoryStore()
    await store.save(MemoryEntry(name="a", description="d", type=MemoryType.user, body="B"))
    await store.delete("a")
    assert await store.read("a") is None
    assert await store.load_index() == []


def test_public_reexports_available():
    import friday_agent.memory as m

    for sym in (
        "MemoryStore", "FileMemoryStore",
        "MemoryEntry", "IndexEntry", "MemoryType",
        "MemorySave", "MemoryRead", "MemoryDelete",
        "build_memory_section",
    ):
        assert hasattr(m, sym), f"friday_agent.memory should re-export {sym}"


# --- Prompt-section assembly (MEMORY_INSTRUCTIONS + auto index) -----------------

@pytest.mark.asyncio
async def test_build_section_lists_each_entry_as_type_name_description():
    store = InMemoryStore()
    await store.save(MemoryEntry(name="user-role", description="backend eng", type=MemoryType.user, body="B"))
    await store.save(MemoryEntry(name="testing-policy", description="real DB", type=MemoryType.feedback, body="B"))
    out = await build_memory_section(store)
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
