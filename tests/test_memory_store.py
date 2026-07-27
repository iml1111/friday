"""Tests for friday_agent/memory/store.py — data model + prompt assembly."""
import pytest

from friday_agent.memory.store import (
    MEMORY_INSTRUCTIONS,
    IndexEntry,
    MemoryEntry,
    MemoryType,
    build_memory_reminder,
)
from friday_agent.messages.types import SYSTEM_REMINDER_PREFIX
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
        "build_memory_reminder",
    ):
        assert hasattr(m, sym), f"friday_agent.memory should re-export {sym}"


# --- Prompt assembly: static instructions vs turn-local index reminder ----------
# MEMORY_INSTRUCTIONS is session-stable -> safe in the cached system prefix.
# The live index changes on every memory_save/delete, so it rides messages[-1]
# as a <system-reminder>; putting it in the system prompt would invalidate the
# whole conversation cache.

@pytest.mark.asyncio
async def test_reminder_renders_index_in_system_reminder_block():
    store = InMemoryStore()
    await store.save(MemoryEntry(name="user-role", description="backend eng", type=MemoryType.user, body="B"))
    await store.save(MemoryEntry(name="testing-policy", description="real DB", type=MemoryType.feedback, body="B"))
    out = await build_memory_reminder(store)
    assert out.startswith("<system-reminder>")
    assert out.rstrip().endswith("</system-reminder>")
    assert "[user] user-role — backend eng" in out
    assert "[feedback] testing-policy — real DB" in out


@pytest.mark.asyncio
async def test_reminder_empty_store_returns_empty_string():
    out = await build_memory_reminder(InMemoryStore())
    assert out == ""


@pytest.mark.asyncio
async def test_reminder_starts_with_shared_detection_prefix():
    # The Anthropic adapter skips cache breakpoints on blocks starting with
    # SYSTEM_REMINDER_PREFIX; a reminder not opening with that exact constant
    # would silently become a dead cache-write target.
    store = InMemoryStore()
    await store.save(MemoryEntry(name="n", description="d", type=MemoryType.user, body="B"))
    out = await build_memory_reminder(store)
    assert out.startswith(SYSTEM_REMINDER_PREFIX)


@pytest.mark.asyncio
async def test_reminder_collapses_multiline_name_and_description():
    # FileMemoryStore keeps descriptions single-line, but the MemoryStore ABC
    # does not guarantee it; the line-oriented index must not break on custom
    # stores returning multi-line text.
    store = InMemoryStore()
    await store.save(MemoryEntry(
        name="multi\nname", description="line one\nline two", type=MemoryType.user, body="B",
    ))
    out = await build_memory_reminder(store)
    assert "- [user] multi name — line one line two" in out


def test_instructions_reference_reminder_location():
    # The instructions must point at the index's new home (a <system-reminder>
    # block near the end of the conversation), not claim it sits "below".
    assert "system-reminder" in MEMORY_INSTRUCTIONS
    assert "below" not in MEMORY_INSTRUCTIONS


def test_instructions_include_read_guidance():
    # The index carries name+description only — the instructions must direct
    # the model to read a relevant memory's body, not act on the index alone.
    assert "Read a memory" in MEMORY_INSTRUCTIONS


def test_instructions_carry_empty_state_nudge():
    # The empty-state nudge lives in the static instructions: an empty store
    # emits no reminder block at all, so "no block" must be interpretable.
    assert "empty" in MEMORY_INSTRUCTIONS
