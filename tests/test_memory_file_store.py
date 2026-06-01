"""Tests for FileMemoryStore — section parse/serialize roundtrip, lazy file creation."""
import time

import pytest

from friday_agent.memory.store import FileMemoryStore, MemoryEntry, MemoryType


def _store(tmp_path):
    return FileMemoryStore(str(tmp_path / "MEM.md"))


@pytest.mark.asyncio
async def test_absent_file_load_index_empty_and_no_file_created(tmp_path):
    path = tmp_path / "MEM.md"
    store = FileMemoryStore(str(path))
    assert await store.load_index() == []
    assert await store.read("x") is None
    assert not path.exists()   # lazy: reading must not create the file


@pytest.mark.asyncio
async def test_save_creates_file_and_roundtrips(tmp_path):
    path = tmp_path / "MEM.md"
    store = FileMemoryStore(str(path))
    await store.save(MemoryEntry(name="user-role", description="backend eng", type=MemoryType.user, body="Go 10y"))
    assert path.exists()
    got = await store.read("user-role")
    assert got is not None
    assert got.body == "Go 10y" and got.type == MemoryType.user and got.description == "backend eng"


@pytest.mark.asyncio
async def test_save_stamps_updated_at_and_persists_it(tmp_path):
    store = _store(tmp_path)
    before = time.time()
    await store.save(MemoryEntry(name="a", description="d", type=MemoryType.user, body="B"))
    reopened = _store(tmp_path)
    got = await reopened.read("a")
    assert got.updated_at is not None
    assert abs(got.updated_at - before) < 5.0


@pytest.mark.asyncio
async def test_load_index_is_metadata_only(tmp_path):
    store = _store(tmp_path)
    await store.save(MemoryEntry(name="a", description="DESC", type=MemoryType.feedback, body="BODY-NOT-IN-INDEX"))
    index = await store.load_index()
    assert len(index) == 1
    assert index[0].name == "a" and index[0].description == "DESC" and index[0].type == MemoryType.feedback


@pytest.mark.asyncio
async def test_upsert_by_name(tmp_path):
    store = _store(tmp_path)
    await store.save(MemoryEntry(name="a", description="d1", type=MemoryType.user, body="B1"))
    await store.save(MemoryEntry(name="a", description="d2", type=MemoryType.user, body="B2"))
    assert len(await store.load_index()) == 1
    assert (await store.read("a")).body == "B2"


@pytest.mark.asyncio
async def test_multiline_body_roundtrips(tmp_path):
    store = _store(tmp_path)
    body = "rule line\n**Why:** reason\n**How to apply:** when X"
    await store.save(MemoryEntry(name="fb", description="d", type=MemoryType.feedback, body=body))
    assert (await _store(tmp_path).read("fb")).body == body


@pytest.mark.asyncio
async def test_two_entries_keep_separate(tmp_path):
    store = _store(tmp_path)
    await store.save(MemoryEntry(name="a", description="da", type=MemoryType.user, body="BA"))
    await store.save(MemoryEntry(name="b", description="db", type=MemoryType.project, body="BB"))
    names = {e.name for e in await store.load_index()}
    assert names == {"a", "b"}
    assert (await store.read("b")).body == "BB"


@pytest.mark.asyncio
async def test_delete_removes_entry(tmp_path):
    store = _store(tmp_path)
    await store.save(MemoryEntry(name="a", description="d", type=MemoryType.user, body="B"))
    await store.delete("a")
    assert await store.read("a") is None
    assert await store.load_index() == []


@pytest.mark.asyncio
async def test_delete_absent_file_is_noop(tmp_path):
    path = tmp_path / "MEM.md"
    store = FileMemoryStore(str(path))
    await store.delete("nonexistent")
    assert not path.exists()


@pytest.mark.asyncio
async def test_unknown_type_entry_is_skipped(tmp_path):
    path = tmp_path / "MEM.md"
    path.write_text(
        "# FRIDAY_MEMORY.md\n\n"
        "## [bogus] bad-one\ndescription: x\n---\nbody\n\n"
        "## [user] good-one\ndescription: y\n---\nok\n",
        encoding="utf-8",
    )
    store = FileMemoryStore(str(path))
    names = {e.name for e in await store.load_index()}
    assert names == {"good-one"}
