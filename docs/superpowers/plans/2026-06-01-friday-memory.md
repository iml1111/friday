# Friday Memory (always-on built-in) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an always-on, replaceable persistent-memory subsystem (`MemoryStore` owning both persistence and its tool surface) to `friday_agent`, wired in at the engine boundary with the core loop left unchanged.

**Architecture:** A new `friday_agent/memory/` subsystem defines `MemoryStore` (ABC owning `save/read/delete/load_index` + a default `tools()`), a default `FileMemoryStore` (→ `FRIDAY_MEMORY.md`), an `InMemoryStore`, three thin tools (`memory_save/read/delete`), and prompt assembly (`MEMORY_INSTRUCTIONS` + auto index). `FridayAgent` (the only engine change) defaults the store to `FileMemoryStore`, registers `store.tools()` alongside built-ins, and rebuilds and prepends a per-turn memory section (async) to the system prompt in `step()`. `run_one_turn`, `assemble_system_prompt`, `LoopState`, and the orchestrator stay byte-unchanged.

**Tech Stack:** Python 3.11+, `pydantic` v2 (tool input schemas), `anyio`/`asyncio`, `pytest` + `pytest-asyncio` (`asyncio_mode=auto`). Tests are keyless via `tests/fakes.py::FakeLLMProvider`.

**Authoritative spec:** `docs/superpowers/specs/2026-06-01-friday-memory-design.md`.

---

## Conventions for the implementer (read once)

- **Git is committed directly to `main` as ordinary per-task commits.** No feature branches, no worktrees.
- **Use explicit `git add <paths>` only.** NEVER run `git add -A`, `git add .`, `git restore`, `git checkout <file>`, `git stash`, or `git reset` on ANY file. The repo carries untracked files that must not be swept in or destroyed.
- Re-check the current branch with `git rev-parse --abbrev-ref HEAD` before each commit (the user sometimes switches branches mid-session). Commit only if on `main`.
- End every commit message with this trailer (exact):
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  ```
- Tests are keyless. Never call a real API. Use `FakeLLMProvider`.
- **Hermeticity:** memory tests MUST use `InMemoryStore()` or `FileMemoryStore(str(tmp_path / "MEM.md"))`. NEVER let a test rely on the default `FileMemoryStore()` path (`FRIDAY_MEMORY.md` in CWD).

---

## File Structure

| File | Responsibility |
|---|---|
| `friday_agent/memory/__init__.py` | Package marker; public re-exports (filled in Task 6) |
| `friday_agent/memory/store.py` | `MemoryType`, `MemoryEntry`, `IndexEntry`, `MemoryStore` (ABC: persistence + `tools()`), `FileMemoryStore`, `InMemoryStore` |
| `friday_agent/memory/tool.py` | `MemorySave`, `MemoryRead`, `MemoryDelete` + input schemas |
| `friday_agent/memory/prompt.py` | `MEMORY_INSTRUCTIONS`, `render_index`, `build_memory_section` |
| `friday_agent/core/engine.py` | (MODIFY) default store, register memory tools, prepend memory section in `step()` |
| `scripts/run_agent.py` | (MODIFY) reflect always-on memory in banner/docstring |
| `docs/architecture/08-memory.md` | (NEW) memory subsystem chapter |
| `docs/architecture/00-overview.md`, `02-tool-orchestration.md`, `CLAUDE.md`, `.gitignore` | (MODIFY) doc sync + ignore the default memory file |
| `tests/test_memory_store.py`, `test_memory_tools.py`, `test_memory_file_store.py`, `test_memory_prompt.py`, `test_memory_engine.py` | (NEW) |

---

## Task 1: Data model + `MemoryStore` ABC + `InMemoryStore`

**Files:**
- Create: `friday_agent/memory/__init__.py`
- Create: `friday_agent/memory/store.py`
- Test: `tests/test_memory_store.py`

- [ ] **Step 1: Create the package marker**

Create `friday_agent/memory/__init__.py`:

```python
"""Persistent memory subsystem.

A MemoryStore owns both persistence (save/read/delete/load_index) and its agent
tool surface (tools()). FridayAgent registers a default FileMemoryStore unless the
caller injects their own MemoryStore. Public re-exports are added in the final task.
"""
from __future__ import annotations
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_memory_store.py`:

```python
"""Tests for friday_agent/memory/store.py — data model + InMemoryStore."""
import pytest

from friday_agent.memory.store import (
    IndexEntry,
    InMemoryStore,
    MemoryEntry,
    MemoryType,
)


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
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/test_memory_store.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'friday_agent.memory.store'`.

- [ ] **Step 4: Write the implementation**

Create `friday_agent/memory/store.py`:

```python
"""MemoryStore ABC + data model + InMemoryStore / FileMemoryStore backends.

A MemoryStore owns BOTH persistence and its agent tool surface. Override only the
persistence methods to swap the backend (default tools() is inherited); override
tools() to change the exposed tool set.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from friday_agent.tools.base import Tool


class MemoryType(str, Enum):
    user = "user"
    feedback = "feedback"
    project = "project"
    reference = "reference"


@dataclass
class MemoryEntry:
    """A full memory (with body); the unit save()/read() operate on."""
    name: str
    description: str
    type: MemoryType
    body: str
    updated_at: float | None = None   # epoch seconds; freshness caveat source


@dataclass
class IndexEntry:
    """Metadata-only memory descriptor for the injected index (no body)."""
    name: str
    description: str
    type: MemoryType
    updated_at: float | None = None


class MemoryStore(ABC):
    """Persistence + tool surface for agent memory (single injection seam)."""

    @abstractmethod
    async def save(self, entry: MemoryEntry) -> None:
        """Upsert by name (same name updates rather than duplicates)."""
        ...

    @abstractmethod
    async def read(self, name: str) -> MemoryEntry | None:
        """Return the full entry (with body), or None if absent."""
        ...

    @abstractmethod
    async def delete(self, name: str) -> None:
        """Remove the entry by name (no error if absent)."""
        ...

    @abstractmethod
    async def load_index(self) -> list["IndexEntry"]:
        """Return metadata-only descriptors (no bodies) for index injection."""
        ...


class InMemoryStore(MemoryStore):
    """Dict-backed store: test double + single-process, non-persistent option."""

    def __init__(self) -> None:
        self._data: dict[str, MemoryEntry] = {}

    async def save(self, entry: MemoryEntry) -> None:
        if entry.updated_at is None:
            entry = replace(entry, updated_at=time.time())
        self._data[entry.name] = entry

    async def read(self, name: str) -> MemoryEntry | None:
        return self._data.get(name)

    async def delete(self, name: str) -> None:
        self._data.pop(name, None)

    async def load_index(self) -> list[IndexEntry]:
        return [
            IndexEntry(name=e.name, description=e.description, type=e.type, updated_at=e.updated_at)
            for e in self._data.values()
        ]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_memory_store.py -q`
Expected: PASS (7 passed).

- [ ] **Step 6: Commit**

```bash
git rev-parse --abbrev-ref HEAD   # must print: main
git add friday_agent/memory/__init__.py friday_agent/memory/store.py tests/test_memory_store.py
git commit -m "feat(memory): MemoryStore ABC + data model + InMemoryStore

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Memory tools + `MemoryStore.tools()` default

**Files:**
- Create: `friday_agent/memory/tool.py`
- Modify: `friday_agent/memory/store.py` (add concrete `tools()` to the ABC)
- Test: `tests/test_memory_tools.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory_tools.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_memory_tools.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'friday_agent.memory.tool'`.

- [ ] **Step 3: Implement the tools**

Create `friday_agent/memory/tool.py`:

```python
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
```

- [ ] **Step 4: Add `tools()` to the `MemoryStore` ABC**

First, add the type-only import that the `tools()` return annotation needs. Near the top of `friday_agent/memory/store.py`, add `TYPE_CHECKING` to the `typing` import and the guarded `Tool` import (this import was intentionally NOT added in Task 1 — it belongs with the method that uses it):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from friday_agent.tools.base import Tool
```

Then add this concrete method to the `MemoryStore` class, immediately after the `load_index` abstract method (the lazy import inside the method avoids a store↔tool circular import at runtime):

```python
    def tools(self) -> list["Tool"]:
        """Tools that surface this store to the agent. Default = save/read/delete
        wrapping self. Override to expose a different tool set (e.g. add search)."""
        from friday_agent.memory.tool import MemoryDelete, MemoryRead, MemorySave

        return [MemorySave(self), MemoryRead(self), MemoryDelete(self)]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_memory_tools.py -q`
Expected: PASS (8 passed).

- [ ] **Step 6: Commit**

```bash
git rev-parse --abbrev-ref HEAD   # must print: main
git add friday_agent/memory/tool.py friday_agent/memory/store.py tests/test_memory_tools.py
git commit -m "feat(memory): save/read/delete tools + MemoryStore.tools() default

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `FileMemoryStore` (default backend → `FRIDAY_MEMORY.md`)

**Files:**
- Modify: `friday_agent/memory/store.py` (add `FileMemoryStore` + parse/serialize helpers)
- Test: `tests/test_memory_file_store.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory_file_store.py`:

```python
"""Tests for FileMemoryStore — section parse/serialize roundtrip, lazy file creation."""
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
    await store.save(MemoryEntry(name="a", description="d", type=MemoryType.user, body="B"))
    # Re-open from disk to prove updated_at survives serialization.
    reopened = _store(tmp_path)
    got = await reopened.read("a")
    assert got.updated_at is not None


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_memory_file_store.py -q`
Expected: FAIL with `ImportError: cannot import name 'FileMemoryStore'`.

- [ ] **Step 3: Implement `FileMemoryStore`**

In `friday_agent/memory/store.py`, extend the imports at the top:

```python
import re
from datetime import datetime
from pathlib import Path
```

Then append the helpers and class at the end of the file:

```python
_FILE_HEADER = (
    "# FRIDAY_MEMORY.md\n"
    "<!-- Friday agent memory. Auto-managed; safe to read/edit by hand. -->\n"
)
_ENTRY_RE = re.compile(r"^##\s+\[(?P<type>\w+)\]\s+(?P<name>.+?)\s*$")


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _from_iso(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def _serialize_entries(entries: list[MemoryEntry]) -> str:
    out = [_FILE_HEADER]
    for e in entries:
        out.append("")
        out.append(f"## [{e.type.value}] {e.name}")
        out.append(f"description: {e.description}")
        iso = _iso(e.updated_at)
        if iso:
            out.append(f"updated: {iso}")
        out.append("---")
        out.append(e.body.rstrip("\n"))
    return "\n".join(out) + "\n"


def _parse_entries(text: str) -> list[MemoryEntry]:
    lines = text.splitlines()
    entries: list[MemoryEntry] = []
    i, n = 0, len(lines)
    while i < n:
        header = _ENTRY_RE.match(lines[i])
        if not header:
            i += 1
            continue
        etype, name = header.group("type"), header.group("name")
        i += 1
        description, updated_at = "", None
        while i < n and lines[i].strip() != "---" and not _ENTRY_RE.match(lines[i]):
            line = lines[i]
            if line.startswith("description:"):
                description = line[len("description:"):].strip()
            elif line.startswith("updated:"):
                try:
                    updated_at = _from_iso(line[len("updated:"):].strip())
                except ValueError:
                    updated_at = None
            i += 1
        if i < n and lines[i].strip() == "---":
            i += 1
        body_lines: list[str] = []
        while i < n and not _ENTRY_RE.match(lines[i]):
            body_lines.append(lines[i])
            i += 1
        entries.append(
            MemoryEntry(
                name=name,
                description=description,
                type=MemoryType(etype),
                body="\n".join(body_lines).strip("\n"),
                updated_at=updated_at,
            )
        )
    return entries


class FileMemoryStore(MemoryStore):
    """Default backend: a single human-readable Markdown file of memory sections.

    Lazy: reads of an absent file return empty; the file is created on first save().
    A single-process / local-dev convenience — for distributed persistence inject a
    MemoryStore backed by your external Storage.
    """

    def __init__(self, path: str = "FRIDAY_MEMORY.md") -> None:
        self._path = Path(path)

    def _read_all(self) -> list[MemoryEntry]:
        if not self._path.exists():
            return []
        return _parse_entries(self._path.read_text(encoding="utf-8"))

    def _write_all(self, entries: list[MemoryEntry]) -> None:
        self._path.write_text(_serialize_entries(entries), encoding="utf-8")

    async def save(self, entry: MemoryEntry) -> None:
        if entry.updated_at is None:
            entry = replace(entry, updated_at=time.time())
        entries = self._read_all()
        for idx, existing in enumerate(entries):
            if existing.name == entry.name:
                entries[idx] = entry
                break
        else:
            entries.append(entry)
        self._write_all(entries)

    async def read(self, name: str) -> MemoryEntry | None:
        for e in self._read_all():
            if e.name == name:
                return e
        return None

    async def delete(self, name: str) -> None:
        entries = [e for e in self._read_all() if e.name != name]
        self._write_all(entries)

    async def load_index(self) -> list[IndexEntry]:
        return [
            IndexEntry(name=e.name, description=e.description, type=e.type, updated_at=e.updated_at)
            for e in self._read_all()
        ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_memory_file_store.py -q`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git rev-parse --abbrev-ref HEAD   # must print: main
git add friday_agent/memory/store.py tests/test_memory_file_store.py
git commit -m "feat(memory): FileMemoryStore default backend (FRIDAY_MEMORY.md)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Prompt assembly — `MEMORY_INSTRUCTIONS` + auto index

**Files:**
- Create: `friday_agent/memory/prompt.py`
- Test: `tests/test_memory_prompt.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory_prompt.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_memory_prompt.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'friday_agent.memory.prompt'`.

- [ ] **Step 3: Implement the prompt module**

Create `friday_agent/memory/prompt.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_memory_prompt.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git rev-parse --abbrev-ref HEAD   # must print: main
git add friday_agent/memory/prompt.py tests/test_memory_prompt.py
git commit -m "feat(memory): MEMORY_INSTRUCTIONS + build_memory_section index assembly

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Engine integration (always-on, replaceable)

**Files:**
- Modify: `friday_agent/core/engine.py`
- Test: `tests/test_memory_engine.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory_engine.py`:

```python
"""Engine integration: always-on memory, replaceable store, collision, compact-clean."""
import pytest

from friday_agent.api.provider import (
    AssistantResponse,
    StopReason,
    TextBlock,
    TokenUsage,
    ToolUseBlock,
)
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState
from friday_agent.memory.prompt import MEMORY_INSTRUCTIONS
from friday_agent.memory.store import InMemoryStore, MemoryStore
from friday_agent.messages.types import create_user_message
from friday_agent.tools.base import Tool, ToolResult
from tests._drive import collect_turn
from tests.fakes import FakeLLMProvider
from pydantic import BaseModel


def _end():
    return AssistantResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN, usage=TokenUsage())


@pytest.mark.asyncio
async def test_default_registers_memory_tools_without_injection():
    fake = FakeLLMProvider(responses=[_end()])
    engine = FridayAgent(provider=fake, tools=[], memory=InMemoryStore())
    await collect_turn(engine, LoopState(messages=[create_user_message("hi")]))
    names = {t["name"] for t in fake.received_tools[0]}
    assert {"memory_save", "memory_read", "memory_delete"} <= names


@pytest.mark.asyncio
async def test_memory_instructions_injected_into_system_prompt():
    fake = FakeLLMProvider(responses=[_end()])
    engine = FridayAgent(provider=fake, tools=[], system_prompt="BASE", memory=InMemoryStore())
    await collect_turn(engine, LoopState(messages=[create_user_message("hi")]))
    sp = fake.received_system_prompts[0]
    assert "BASE" in sp
    assert MEMORY_INSTRUCTIONS in sp


def test_caller_tool_colliding_with_memory_name_raises():
    class Clash(Tool):
        name = "memory_save"
        def input_schema(self):
            class I(BaseModel):
                pass
            return I
        async def call(self, args):
            return ToolResult(data="x")

    with pytest.raises(ValueError, match="memory_save"):
        FridayAgent(provider=FakeLLMProvider(responses=[]), tools=[Clash()], memory=InMemoryStore())


@pytest.mark.asyncio
async def test_injected_store_tools_replace_defaults():
    class Recall(Tool):
        name = "my_recall"
        def input_schema(self):
            class I(BaseModel):
                pass
            return I
        async def call(self, args):
            return ToolResult(data="x")

    class CustomStore(InMemoryStore):
        def tools(self):
            return [Recall()]

    fake = FakeLLMProvider(responses=[_end()])
    engine = FridayAgent(provider=fake, tools=[], memory=CustomStore())
    await collect_turn(engine, LoopState(messages=[create_user_message("hi")]))
    names = {t["name"] for t in fake.received_tools[0]}
    assert "my_recall" in names
    assert "memory_save" not in names   # default surface fully replaced


@pytest.mark.asyncio
async def test_compact_does_not_inject_memory_section():
    summary = AssistantResponse(
        content=[TextBlock(text="<summary>S</summary>")],
        stop_reason=StopReason.END_TURN, usage=TokenUsage(),
    )
    fake = FakeLLMProvider(responses=[summary])
    engine = FridayAgent(provider=fake, tools=[], system_prompt="BASE", memory=InMemoryStore())
    await engine.compact(LoopState(messages=[create_user_message("a")], turn_count=1))
    assert MEMORY_INSTRUCTIONS not in fake.received_system_prompts[0]


@pytest.mark.asyncio
async def test_memory_save_tool_persists_to_injected_store():
    save = AssistantResponse(
        content=[ToolUseBlock(
            id="m1", name="memory_save",
            input={"name": "user-role", "description": "backend eng", "type": "user", "body": "Go 10y"},
        )],
        stop_reason=StopReason.TOOL_USE, usage=TokenUsage(),
    )
    store = InMemoryStore()
    engine = FridayAgent(provider=FakeLLMProvider(responses=[save, _end()]), tools=[], memory=store)
    await collect_turn(engine, LoopState(messages=[create_user_message("remember my role")]))
    got = await store.read("user-role")
    assert got is not None and got.body == "Go 10y"


@pytest.mark.asyncio
async def test_memory_body_does_not_leak_into_loopstate_serde():
    save = AssistantResponse(
        content=[ToolUseBlock(
            id="m1", name="memory_save",
            input={"name": "secret", "description": "d", "type": "user", "body": "SENSITIVE-BODY"},
        )],
        stop_reason=StopReason.TOOL_USE, usage=TokenUsage(),
    )
    engine = FridayAgent(provider=FakeLLMProvider(responses=[save, _end()]), tools=[], memory=InMemoryStore())
    _, outcome = await collect_turn(engine, LoopState(messages=[create_user_message("save it")]))
    assert isinstance(outcome, LoopState)
    # LoopState serde is unchanged: only messages/turn_count/todos; the store is container-local.
    assert set(outcome.to_dict().keys()) == {"messages", "turn_count", "todos"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_memory_engine.py -q`
Expected: FAIL — `FridayAgent.__init__() got an unexpected keyword argument 'memory'`.

- [ ] **Step 3: Add imports to `friday_agent/core/engine.py`**

After the existing `from friday_agent.tools.builtin import builtin_tools` line (currently `engine.py:20`), add:

```python
from friday_agent.memory.prompt import build_memory_section
from friday_agent.memory.store import FileMemoryStore, MemoryStore
```

- [ ] **Step 4: Modify `__init__` — add `memory` param, default store, generalized uniqueness check**

In `friday_agent/core/engine.py`, add the `memory` parameter to the signature (after `max_concurrency: int = 10,`):

```python
        memory: MemoryStore | None = None,
```

Then REPLACE the current tool-assembly block (currently `engine.py:64-77`, from `caller_tools = ...` through `self._max_concurrency = max_concurrency`) with:

```python
        self._memory = memory if memory is not None else FileMemoryStore()
        caller_tools = tools if tools is not None else []
        assembled = [*caller_tools, *builtin_tools(), *self._memory.tools()]
        seen: set[str] = set()
        dups: list[str] = []
        for t in assembled:
            if t.name in seen:
                dups.append(t.name)
            seen.add(t.name)
        if dups:
            raise ValueError(
                f"FridayAgent: duplicate tool names {sorted(set(dups))}. TodoWrite and the "
                f"active MemoryStore's tools are SDK-managed and always registered; remove "
                f"the colliding tool(s) or override the store's tools()."
            )
        self._provider = provider
        self._tools = assembled
        self._system_prompt = system_prompt
        self._config = config
        self._max_concurrency = max_concurrency
```

- [ ] **Step 5: Modify `step()` — prepend the per-turn memory section**

In `friday_agent/core/engine.py`, at the START of the `step()` body (before `tool_schemas = ...`), insert:

```python
        memory_section = await build_memory_section(self._memory)
        effective_prompt = (
            f"{self._system_prompt}\n\n{memory_section}"
            if self._system_prompt
            else memory_section
        )
```

Then change the `run_one_turn(...)` call's `system_prompt=self._system_prompt,` argument to:

```python
            system_prompt=effective_prompt,
```

(Leave `compact()` untouched — it must NOT inject the memory section.)

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `python -m pytest tests/test_memory_engine.py -q`
Expected: PASS (7 passed).

- [ ] **Step 7: Run the FULL suite to verify no regressions**

Run: `python -m pytest -q`
Expected: PASS. Existing `step()`-path assertions use subset checks and tolerate the added memory tools/section; compact-path exact assertions are unaffected (compact does not inject memory). `tests/test_builtin_injection.py::test_caller_injecting_builtin_name_raises` still passes (the generalized message still contains `TodoWrite`). Total = previous count + 34 new memory tests.

If any pre-existing test fails, STOP and report it — do not edit unrelated tests to force green without understanding the cause.

- [ ] **Step 8: Commit**

```bash
git rev-parse --abbrev-ref HEAD   # must print: main
git add friday_agent/core/engine.py tests/test_memory_engine.py
git commit -m "feat(memory): always-on engine integration (default store, tool registration, section injection)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Downstream wiring + docs + ignore file + public exports

**Files:**
- Modify: `friday_agent/memory/__init__.py` (public re-exports)
- Modify: `scripts/run_agent.py` (banner/docstring)
- Modify: `.gitignore` (ignore default memory file)
- Create: `docs/architecture/08-memory.md`
- Modify: `docs/architecture/00-overview.md`, `docs/architecture/02-tool-orchestration.md`, `CLAUDE.md`

- [ ] **Step 1: Write the failing test (public exports)**

Append to `tests/test_memory_store.py`:

```python
def test_public_reexports_available():
    import friday_agent.memory as m

    for sym in (
        "MemoryStore", "FileMemoryStore", "InMemoryStore",
        "MemoryEntry", "IndexEntry", "MemoryType",
        "MemorySave", "MemoryRead", "MemoryDelete",
        "build_memory_section",
    ):
        assert hasattr(m, sym), f"friday_agent.memory should re-export {sym}"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_memory_store.py::test_public_reexports_available -q`
Expected: FAIL (`AttributeError` / missing attribute).

- [ ] **Step 3: Fill in `friday_agent/memory/__init__.py`**

Replace the file body (keep the docstring) so it re-exports the public API:

```python
"""Persistent memory subsystem.

A MemoryStore owns both persistence (save/read/delete/load_index) and its agent
tool surface (tools()). FridayAgent registers a default FileMemoryStore unless the
caller injects their own MemoryStore.
"""
from __future__ import annotations

from friday_agent.memory.prompt import MEMORY_INSTRUCTIONS, build_memory_section, render_index
from friday_agent.memory.store import (
    FileMemoryStore,
    IndexEntry,
    InMemoryStore,
    MemoryEntry,
    MemoryStore,
    MemoryType,
)
from friday_agent.memory.tool import MemoryDelete, MemoryRead, MemorySave

__all__ = [
    "MemoryStore",
    "FileMemoryStore",
    "InMemoryStore",
    "MemoryEntry",
    "IndexEntry",
    "MemoryType",
    "MemorySave",
    "MemoryRead",
    "MemoryDelete",
    "MEMORY_INSTRUCTIONS",
    "render_index",
    "build_memory_section",
]
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python -m pytest tests/test_memory_store.py::test_public_reexports_available -q`
Expected: PASS.

- [ ] **Step 5: Update `scripts/run_agent.py` banner + docstring**

In the module docstring, after the `TodoWrite is a built-in tool...` paragraph, add:

```
Memory is always-on too: FridayAgent registers memory_save / memory_read /
memory_delete from a default FileMemoryStore, which persists to FRIDAY_MEMORY.md in
the working directory across runs. Inject your own MemoryStore (FridayAgent(...,
memory=YourStore())) to replace the backend and its tools.
```

Change the banner print line (`scripts/run_agent.py:95`) to mention memory:

```python
    print(f"friday-agent local example — model={_MODEL}  (tools: ExampleTool; TodoWrite + memory_* are built-in)")
```

- [ ] **Step 6: Ignore the default memory file**

Append to `.gitignore` (create the file if it does not exist):

```
# Default FileMemoryStore output (local dev convenience)
FRIDAY_MEMORY.md
```

- [ ] **Step 7: Create `docs/architecture/08-memory.md`**

Create the chapter (mirror the style of existing `0X-*.md` chapters):

```markdown
# 08 — 메모리 (영속 메모리 서브시스템)

> **관련 문서**: [00-overview](00-overview.md) · [01-core-loop](01-core-loop.md) · [02-tool-orchestration](02-tool-orchestration.md) · [03-llm-providers](03-llm-providers.md)

---

## ① 목적

세션 경계를 넘어 타입화된 fact(user/feedback/project/reference)를 장기화한다. **always-on 빌트인**이며, 호출자가 자신의 `MemoryStore`를 주입하면 기본 store와 도구가 통째로 대체된다.

## ② 담당 파일

| 경로 | 책임 | 핵심 심볼 |
|---|---|---|
| `friday_agent/memory/store.py` | 영속 + 도구 표면 인터페이스, 기본/메모리 백엔드 | `MemoryStore`, `FileMemoryStore`, `InMemoryStore`, `MemoryEntry`, `IndexEntry`, `MemoryType` |
| `friday_agent/memory/tool.py` | 에이전트 표면(저장/읽기/삭제) | `MemorySave`, `MemoryRead`, `MemoryDelete` |
| `friday_agent/memory/prompt.py` | 지침 + 자동 인덱스 주입 텍스트 | `MEMORY_INSTRUCTIONS`, `build_memory_section`, `render_index` |

## ③ 단일 주입 seam — `MemoryStore`

`MemoryStore`(ABC)가 **영속(save/read/delete/load_index) + `tools()`**를 모두 소유한다. 백엔드만 교체하려면 영속 4메서드만 구현(기본 `tools()` 상속), 도구까지 교체하려면 `tools()`를 오버라이드한다. 기본 도구는 `memory_save`/`memory_read`/`memory_delete`이며 일반 `ToolResult`만 반환한다(메모리는 LoopState가 아니라 Store에 산다).

## ④ 엔진 통합 (루프 무변경)

`FridayAgent.__init__`이 기본 `FileMemoryStore`를 세팅하고 `store.tools()`를 빌트인·호출자 도구와 함께 등록한다(이름 충돌 시 `ValueError`). `step()`은 매 턴 `build_memory_section()`을 async로 조립해(턴별 재구성, 캐시 없음) base 시스템 프롬프트 뒤에 덧붙인다. `run_one_turn`·`assemble_system_prompt`·`LoopState`·orchestrator는 불변. `compact()`는 메모리 섹션을 주입하지 않아 인덱스가 요약에 새지 않는다.

## ⑤ 분산 안전

`MemoryStore`는 `LoopState`에 직렬화되지 않는다(provider/tools와 동일 컨테이너-로컬 재주입). 컨테이너 B가 store로 `FridayAgent`를 재구성하면 섹션이 재조립돼 인덱스가 fresh 반영된다. 기본 `FileMemoryStore`는 단일 프로세스/로컬 편의 — 진짜 분산 영속은 외부 Storage 기반 `MemoryStore`를 주입한다.

## ⑥ 비목표

Sonnet prefetch 랭킹 · 백그라운드 포크 추출 · 팀 메모리/시크릿 스캔/scope 태그 제외. (인덱스는 `step()`마다 `build_memory_section`이 재조립한다 — 캐시 없음.) `search`는 기본 도구가 아니며 커스텀 store가 `tools()`로 노출할 수 있다.
```

- [ ] **Step 8: Update `docs/architecture/00-overview.md` module map**

In the module map section, add a row/line for the memory subsystem next to the other modules (e.g. after the `tools/` or `context/` entry):

```
- `memory/` — 영속 메모리 서브시스템(store·tool·prompt). always-on 빌트인, `MemoryStore` 주입으로 교체. 상세: [08-memory](08-memory.md).
```

(Match the exact list/table format already used in that file.)

- [ ] **Step 9: Update `docs/architecture/02-tool-orchestration.md` built-in note**

In §⑥, extend the existing **빌트인 도구 자동 등록** paragraph with a sentence:

```
또한 활성 `MemoryStore`의 `tools()`(기본 `memory_save`/`memory_read`/`memory_delete`)도 항상 등록되며, 호출자 도구가 빌트인 또는 메모리 도구 이름과 겹치면 `__init__`이 `ValueError`로 거부한다(전체 도구 이름 유일성 검사).
```

- [ ] **Step 10: Update `CLAUDE.md`**

Make these edits:

1. Replace the `MemoryProvider` line (currently line ~76, under "LLM 추상화 경계"):
   - FROM: `- **`MemoryProvider`** — 옵션 (영속 메모리 백엔드, 미지원 시 NoOp).`
   - TO: `- **메모리는 별도 서브시스템**(LLM 경계 아님) — `friday_agent/memory/`의 `MemoryStore`(영속 + `tools()` 소유). always-on 빌트인이며 주입으로 교체. 상세: `docs/architecture/08-memory.md`.`

2. In "## 타겟 스택 & 검증", add a bullet after the built-in todo bullet:
   ```
   - **빌트인 memory 항상-주입**: 기본 `FileMemoryStore`(→`FRIDAY_MEMORY.md`)와 `memory_save`/`memory_read`/`memory_delete`가 항상 등록되고, `MEMORY_INSTRUCTIONS`+자동 인덱스가 `step()`에서 시스템 프롬프트에 주입된다. 호출자가 `FridayAgent(..., memory=MyStore())`로 주입하면 store와 도구가 통째로 대체된다. 코어 루프·`LoopState` serde는 불변(store는 컨테이너-로컬 재주입).
   ```

3. In the package-structure line near the end, add `memory/`(store·tool·prompt) to the module list.

- [ ] **Step 11: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all tests green).

- [ ] **Step 12: Commit**

```bash
git rev-parse --abbrev-ref HEAD   # must print: main
git add friday_agent/memory/__init__.py scripts/run_agent.py .gitignore \
        docs/architecture/08-memory.md docs/architecture/00-overview.md \
        docs/architecture/02-tool-orchestration.md CLAUDE.md tests/test_memory_store.py
git commit -m "docs(memory): public exports, run_agent demo, architecture + CLAUDE sync

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review (plan vs spec)

**1. Spec coverage:**
- §3 모듈 구조 → Tasks 1-4 (store/tool/prompt) + Task 6 (`__init__` exports). ✓
- §4 데이터 모델 (MemoryType/MemoryEntry/IndexEntry) → Task 1. ✓
- §5 `MemoryStore` ABC + `tools()` + FileMemoryStore + InMemoryStore → Tasks 1-3. ✓
- §6 엔진 통합 (memory param, default store, generalized uniqueness check, lazy section, step prepend, compact untouched) → Task 5. ✓
- §7 prompt (MEMORY_INSTRUCTIONS/render_index/build_memory_section) → Task 4. ✓
- §8 도구 (save/read/delete, no state_effect, read concurrency-safe, staleness caveat) → Task 2. ✓
- §10 불변식 (loop unchanged, serde-clean, compact-clean) → Task 5 tests. ✓
- §11 하위 정리 (run_agent, 08-memory, 00-overview, 02-tool-orchestration, CLAUDE.md) → Task 6. ✓
- §12 테스트 전략 (unit store/tool/prompt, integration, distributed serde-clean, regression) → Tasks 1-6. ✓
- **Gap check:** `.gitignore FRIDAY_MEMORY.md` (hermeticity) is an addition beyond the spec but required by the "always-on default writes to CWD" reality — included in Task 6.

**2. Placeholder scan:** No "TBD"/"TODO"/"handle edge cases"/"similar to Task N". Every code step has complete code; every test step has runnable assertions. ✓

**3. Type/name consistency:** `MemoryStore`, `MemoryEntry(name, description, type, body, updated_at)`, `IndexEntry(name, description, type, updated_at)`, `MemoryType`, tool names `memory_save`/`memory_read`/`memory_delete`, `build_memory_section(store)`, `MEMORY_INSTRUCTIONS`, `render_index(entries)`, engine `memory=` param — all consistent across Tasks 1-6 and match the spec. `Tool` base import path `friday_agent.tools.base` and `ToolResult(data=..., is_error=...)` match the existing codebase. `is_concurrency_safe(self, input_data: dict)` signature matches `TodoWrite`. ✓

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-01-friday-memory.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — fresh subagent per task, two-stage review (spec compliance then code quality) between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session with checkpoints.

**Which approach?**
